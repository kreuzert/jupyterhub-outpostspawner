# Usage

```{css, echo=FALSE}
pre {
  max-height: 300px;
  overflow-y: auto;
}
```

This section contains example configurations and instructions, to use [zero2jupyterhub](https://z2jh.jupyter.org) with the OutpostSpawner and two [JupyterHub Outposts](https://artifacthub.io/packages/helm/jupyterhub-outpost/jupyterhub-outpost). We will use two Kubernetes clusters: "local" (JupyterHub and Outpost will be installed) and "remote" (only Outpost will be installed). For JupyterHub the namespace `jupyter` is used. For Outpost the namespace `outpost` is used.

```{admonition} Warning
In this example the communication between "local" and "remote" is not encrypted. Do not use this setup in production.
You can use ingress-nginx on the remote cluster to enable encryption. You'll find an example at the end of this section.
```

## Pre-Requirements

2 Kubernetes clusters up and running. In this example we will use [ingress-nginx](https://artifacthub.io/packages/helm/ingress-nginx/ingress-nginx) on the local cluster.  
The Outpost on the "remote" cluster must be reachable on port 30080 and 30022 (you can change the ports, or use ingress + LoadBalancer and port 443 + 22).  


## Requirements

To allow JupyterHub to create ssh port forwarding to the Outpost, a ssh keypair is required.

```
ssh-keygen -f jupyterhub-sshkey -t ed25519 -N ''

# On local cluster:
kubectl -n jupyter create secret generic --type=kubernetes.io/ssh-auth --from-file=ssh-privatekey=jupyterhub-sshkey --from-file=ssh-publickey=jupyterhub-sshkey.pub jupyterhub-outpost-sshkey
```

To authenticate the JupyterHub instance at the two outposts, we have to create username+password. In this example we use different a username/password combination for each Outpost. If one Outpost should be connected to multiple JupyterHubs, each JupyterHub needs its own username.

```
LOCAL_OUTPOST_PASSWORD=$(uuidgen)
REMOTE_OUTPOST_PASSWORD=$(uuidgen)

# On local cluster:
## Store username / password for Outpost
kubectl -n outpost create secret generic --from-literal=usernames=jupyterhub --from-literal=passwords=${LOCAL_OUTPOST_PASSWORD} outpost-users

## Store both usernames / passwords for JupyterHub
kubectl -n jupyter create secret generic --from-literal=AUTH_OUTPOST_LOCAL=$(echo -n "jupyterhub:${LOCAL_OUTPOST_PASSWORD}" | base64 -w 0) --from-literal=AUTH_OUTPOST_REMOTE=$(echo -n "jupyterhub:${REMOTE_OUTPOST_PASSWORD}" | base64 -w 0) jupyterhub-outpost-auth

# On remote cluster:
## Store username / password for Outpost
kubectl -n outpost create secret generic --from-literal=usernames=jupyterhub --from-literal=passwords=${REMOTE_OUTPOST_PASSWORD} outpost-users
```

## Create Outpost

Now we're installing the two JupyterHub Outposts. We're using a NodePort service for the remote Outpost, so JupyterHub can reach it. 

For the Outpost we need an encryption key, so data in the database can be encrypted.

```
LOCAL_SECRET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
REMOTE_SECRET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')

# On local cluster:
kubectl -n outpost create secret generic outpost-cryptkey --from-literal=secret_key=${LOCAL_SECRET_KEY}

# On remote cluster:
kubectl -n outpost create secret generic outpost-cryptkey --from-literal=secret_key=${REMOTE_SECRET_KEY}
```

```
cat <<EOF >> outpost_local_values.yaml
cryptSecret: outpost-cryptkey
outpostUsers: outpost-users
sshPublicKeys:
  - $(cat jupyterhub-sshkey.pub)
EOF
```

```
cat <<EOF >> outpost_remote_values.yaml
cryptSecret: outpost-cryptkey
outpostUsers: outpost-users
sshPublicKeys:
  - $(cat jupyterhub-sshkey.pub)
service:
  type: NodePort
  ports:
    nodePort: 30080
servicessh:
  type: NodePort
  ports:
    nodePort: 30022
EOF
```

Now let's install the Outpost on both cluster.
```
# On local cluster
helm repo add jupyterhub-outpost https://kreuzert.github.io/jupyterhub-outpost/charts/
helm repo update
helm upgrade --install --version <version> --namespace outpost -f outpost_local_values.yaml outpost jupyterhub-outpost/jupyterhub-outpost

# On remote cluster
helm upgrade --install --version <version> --namespace outpost -f outpost_remote_values.yaml outpost jupyterhub-outpost/jupyterhub-outpost
```

Ensure that everything is running. Double check if the remote Cluster has opened the ports 30080 and 30022 for the local cluster. Figure out at which IP Adresse JupyterHub will be able to reach the remote Outpost. 
```
# In a NodePort scenario both may be the same. If you're using LoadBalancers they should be different.
# If you're using ingress you can use the DNS alias name, too.
REMOTE_IP_ADDRESS=10.0.123.123 # You have to change this!
REMOTE_IP_ADDRESS_SSH=10.0.123.123 # You have to change this!
```

With these secrets created, we can now start JupyterHub. In this scenario we're using ingress-nginx and disabling a few things, that are not required in this example. Your JupyterHub configuration might look a bit different. 

```
cat <<EOF >> z2jh_values.yaml
hub:
  args:
  - pip install jupyterhub-outpostspawner; jupyterhub --config /usr/local/etc/jupyterhub/jupyterhub_config.py
  command:
  - sh
  - -c
  - --
  config:
    JupyterHub:
      allow_named_servers: true
      default_url: /hub/home
  extraVolumes:
  - name: jupyterhubOutpostSSHKey
    secret:
      secretName: jupyterhub-outpost-sshkey
  extraVolumeMounts:
  - name: jupyterhubOutpostSSHKey
    mountPath: /mnt/ssh_keys
  extraEnv:
  - name: AUTH_OUTPOST_LOCAL
    valueFrom:
      secretKeyRef:
        name: jupyterhub-outpost-auth
        key: AUTH_OUTPOST_LOCAL
  - name: AUTH_OUTPOST_REMOTE
    valueFrom:
      secretKeyRef:
        name: jupyterhub-outpost-auth
        key: AUTH_OUTPOST_REMOTE
  extraConfig:
    customConfig: |-
      import outpostspawner
      c.JupyterHub.spawner_class = outpostspawner.OutpostSpawner
      c.OutpostSpawner.options_form = """
        <label for=\"system\">Choose a system:</label>
        <select name=\"system\">
          <option value="local">local</option>
          <option value="remote">remote</option>
        </select>
      """

      async def request_url(spawner):
        system = spawner.user_options.get("system", "None")
        if system == "local":
          ret = "http://outpost.outpost.svc:8080/services"
        elif system == "remote":
          ret = "http://${REMOTE_IP_ADDRESS}:30080/services"
        else:
          ret = "System not supported"
        spawner.log.info(f"URL for system {system}: {ret}")
        return ret
      c.OutpostSpawner.request_url = request_url

      async def request_headers(spawner):
        system = spawner.user_options.get("system", "None")
        spawner.log.info(f"Create request header for system {system}")
        auth = os.environ.get(f"AUTH_OUTPOST_{system.upper()}")
        return {
          "Authorization": f"Basic {auth}",
          "Accept": "application/json",
          "Content-Type": "application/json"
        }
      c.OutpostSpawner.request_headers = request_headers

      async def ssh_node(spawner):
        system = spawner.user_options.get("system", "None")
        if system == "local":
          ret = "outpost.outpost.svc"
        elif system == "remote":
          ret = "${REMOTE_IP_ADDRESS_SSH}"
        else:
          ret = "System not supported"
        spawner.log.info(f"SSH Node for system {system}: {ret}")
        return ret
      c.OutpostSpawner.ssh_node = ssh_node

      c.OutpostSpawner.ssh_key = "/mnt/ssh_keys/ssh-privatekey"
ingress:
  annotations:
    acme.cert-manager.io/http01-edit-in-place: "false"
    cert-manager.io/cluster-issuer: letsencrypt-cluster-issuer
  enabled: true
  hosts:
  - myjupyterhub.com
  tls:
  - hosts:
    - myjupyterhub.com
    secretName: jupyterhub-tls-certmanager
prePuller:
  continuous:
    enabled: false
  hook:
    enabled: false
proxy:
  service:
    type: ClusterIP
scheduling:
  userScheduler:
    enabled: false
EOF
```

Install JupyterHub on local cluster:

```
# On local cluster:
helm repo add jupyterhub https://hub.jupyter.org/helm-chart/
helm repo update
helm upgrade --cleanup-on-fail --install --namespace jupyter -f z2jh_values.yaml jupyterhub jupyterhub/jupyterhub
```

After a few minutes everything should be up and running. If you have any problems following this example, or want to leave feedback, feel free to open an issue on GitHub. 
You are now able to start JupyterLabs on both Kubernetes Clusters, using the KubeSpawner as default. You will find more information about the possibilities of the Outpost and the OutpostSpawner in this documentation.


## Encryption on JupyterHub Outpost
When running JupyterHub Outpost on production, you should ensure some encryption. An easy way is to use ingress-nginx with a certificate.
For this example we've installed [cert-manager, hairpin-proxy and let's encrypt issuer](https://gitlab.jsc.fz-juelich.de/kaas/fleet-deployments/-/tree/cert-manager). If you already have an certificate you will not need this.

```
FLOATING_IP_SSH=<EXTERNAL_IP_FOR_SSH_ACCESS>
cat <<EOF >> outpost_remote_values_ingress.yaml
cryptSecret: outpost-cryptkey
outpostUsers: outpost-users
sshPublicKeys:
  - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAI81YC6vb0G/fvMY0S61nLGSCmn/wwPdEC3FVBypHTj ubuntu@zam943
servicessh:
  type: LoadBalancer
  loadBalancerIP: ${FLOATING_IP_SSH}
ingress:
  enabled: true
  annotations:
    acme.cert-manager.io/http01-edit-in-place: "false"
    cert-manager.io/cluster-issuer: letsencrypt-cluster-issuer
  hosts:
  - myremoteoutpost.com
  tls:
  - hosts:
    - myremoteoutpost.com
    secretName: outpost-tls-certmanager
EOF
```


## Configure persistent database to Outpost
See [documentation on ArtifactHub](https://artifacthub.io/packages/helm/jupyterhub-outpost/jupyterhub-outpost#configure-database)
