# Usage

This section covers an example configuration to use [Zero2JupyterHub](https://z2jh.jupyter.org) with the OutpostSpawner.  
In this scenario, we will connect the JupyterHub OutpostSpawner with two running JupyterHub Outpost services.  
You can find a tutorial how to install a JupyterHub Outpost service [here](https://jupyterhub-outpost.readthedocs.io/en/latest/usage/installation.html).

```{admonition} Warning
In this example, the communication between JupyterHub and the `second` system (a remote Kubernetes cluster hosting the JupyterHub Outpost service) is not encrypted. Do not use this setup in production.
You can use an Ingress controller such as [ingress-nginx](https://artifacthub.io/packages/helm/ingress-nginx/ingress-nginx) on the JupyterHub Outpost cluster to enable encryption. 
```

## Requirements

One Kubernetes cluster up and running.  
In this example we will use [ingress-nginx](https://artifacthub.io/packages/helm/ingress-nginx/ingress-nginx) as the Ingress controller.

## Preparations

To allow JupyterHub to create ssh port forwarding processes to the Outpost, a ssh keypair is required. Assuming that JupyterHub is to be installed in the `jupyter` namespace, we create the following Kubernetes secret:

```bash
ssh-keygen -f jupyterhub-sshkey -t ed25519 -N ''

kubectl -n jupyter create secret generic --type=kubernetes.io/ssh-auth --from-file=ssh-privatekey=jupyterhub-sshkey --from-file=ssh-publickey=jupyterhub-sshkey.pub jupyterhub-outpost-sshkey
```

To authenticate the JupyterHub instance at JupyterHub Outposts, we receive a username / password combination from each JupyterHub Outpost administrator.

```bash
FIRST_OUTPOST_PASSWORD=... # you should get this from the Outpost administrator
SECOND_OUTPOST_PASSWORD=... # you should get this from the Outpost administrator

## Store both usernames / passwords for JupyterHub
kubectl --namespace jupyter create secret generic --from-literal=AUTH_OUTPOST_FIRST=$(echo -n "jupyterhub:${FIRST_OUTPOST_PASSWORD}" | base64 -w 0) --from-literal=AUTH_OUTPOST_SECOND=$(echo -n "jupyterhub:${SECOND_OUTPOST_PASSWORD}" | base64 -w 0) jupyterhub-outpost-auth
```

## Configuration

With these secrets created, we can now start JupyterHub. In this scenario, we're using ingress-nginx and disabling a few things that are not required for a minimal example setup. Your JupyterHub configuration might look a bit different. 

```{admonition} Warning
We're connecting this JupyterHub with two JupyterHub Outposts. One is running on the same cluster as JupyterHub, the second one is running remotely on a different cluster.  
Therefore, we're using an internal cluster address for the first Outpost. Furthermore, there is no need to enable ssh port-forwarding for the first cluster, as the JupyterLabs will be directly reachable for JupyterHub thanks to Kubernetes' internal DNS name resolution.  
  
All JupyterLabs will be using the external DNS alias name of the JupyterHub to reach the hub api url (see `c.OutpostSpawner.public_api_url`). You might have to install a hairpin-proxy (e.g. [this](https://github.com/compumike/hairpin-proxy)) to allow pods within your cluster to communicate with the public DNS alias name.
```

```yaml
cat <<EOF >> z2jh_values.yaml
hub:
  args:
  # Install OutpostSpawner before starting JupyterHub
  - pip install jupyterhub-outpostspawner; jupyterhub --config /usr/local/etc/jupyterhub/jupyterhub_config.py
  command:
  - sh
  - -c
  - --
  config:
    JupyterHub:
      allow_named_servers: true
      default_url: /hub/home
  # Mount the port forwarding ssh keypair from its secret
  extraVolumes:
  - name: jupyterhub-outpost-sshkey
    secret:
      secretName: jupyterhub-outpost-sshkey
  extraVolumeMounts:
  - name: jupyterhub-outpost-sshkey
    mountPath: /mnt/ssh_keys
  # Define Outpost username/passwords environment variables from secrets
  extraEnv:
  - name: AUTH_OUTPOST_FIRST
    valueFrom:
      secretKeyRef:
        name: jupyterhub-outpost-auth
        key: AUTH_OUTPOST_FIRST
  - name: AUTH_OUTPOST_SECOND
    valueFrom:
      secretKeyRef:
        name: jupyterhub-outpost-auth
        key: AUTH_OUTPOST_SECOND
  extraConfig:
    # Configure Jupyterhub to use the OutpostSpawner
    customConfig: |-
      import outpostspawner
      c.JupyterHub.spawner_class = outpostspawner.OutpostSpawner
      c.OutpostSpawner.options_form = """
        <label for=\"system\">Choose a system:</label>
        <select name=\"system\">
          <option value="first">First</option>
          <option value="second">Second</option>
        </select>
      """

      async def request_url(spawner):
        system = spawner.user_options.get("system", "None")[0]
        if system == "first":
          # Internal cluster address
          ret = "http://outpost.outpost.svc:8080/services"
        elif system == "second":
          # Address of external outpost
          ret = "http://${SECOND_OUTPOST_ADDRESS}/services"
        else:
          ret = "System not supported"
        return ret
      c.OutpostSpawner.request_url = request_url

      async def request_headers(spawner):
        system = spawner.user_options.get("system", "None")[0]
        # Get authentication token from environment variable for each Outpost
        auth = os.environ.get(f"AUTH_OUTPOST_{system.upper()}")
        return {
          "Authorization": f"Basic {auth}",
          "Accept": "application/json",
          "Content-Type": "application/json"
        }
      c.OutpostSpawner.request_headers = request_headers

      async def ssh_node(spawner):
        system = spawner.user_options.get("system", "None")[0]
        if system == "first":
          # Internal cluster address
          ret = "outpost.outpost.svc"
        elif system == "second":
           # Address of SSH service of external outpost
          ret = "${REMOTE_OUTPOST_IP_ADDRESS_SSH}"
        else:
          ret = "System not supported"
        return ret
      c.OutpostSpawner.ssh_node = ssh_node

      def ssh_enabled(spawner):
        system = spawner.user_options.get("system", ["None"])[0]
        if system == "first":
          # No ssh port-forwarding needed for Outpost on the same cluster
          return False
        elif system == "second":
          return True
        else:
          raise Exception("Not supported")
      c.OutpostSpawner.ssh_enabled = ssh_enabled

      # Check https://jupyterhub-outpostspawner.readthedocs.io/en/latest/spawners/outpostspawner.html
      # for an in-depth explanation of each option
      c.OutpostSpawner.ssh_key = "/mnt/ssh_keys/ssh-privatekey"
      c.OutpostSpawner.http_timeout = 1200
      c.OutpostSpawner.public_api_url = "https://myjupyterhub.com/hub/api"
      c.OutpostSpawner.ssh_key = "/mnt/ssh_keys/ssh-privatekey"
      helm_release_name = os.environ.get("HELM_RELEASE_NAME")
      c.OutpostSpawner.svc_name_template = f"{helm_release_name}-{{servername}}-{{userid}}"
# The following options are regular JupyterHub configuration options
# that are not directly related to the OutpostSpawner
ingress:
  # Annotations for using LetsEncrypt as a certificate issuer
  annotations:
    acme.cert-manager.io/http01-edit-in-place: "false"
    cert-manager.io/cluster-issuer: letsencrypt-cluster-issuer
  enabled: true
  hosts:
  - myjupyterhub.com
  tls:
  - hosts:
    - myjupyterhub.com
    # If using LetsEncrypt, the secret will be created automatically. Otherwise, please ensure the secret exists.
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

## Installation

Install JupyterHub:

```
# Add JupyterHub chart repository
helm repo add jupyterhub https://hub.jupyter.org/helm-chart/
helm repo update
# Install JupyterHub  in the `jupyter` namespace
helm upgrade --cleanup-on-fail --install --namespace jupyter -f z2jh_values.yaml jupyterhub jupyterhub/jupyterhub
```

After a few minutes everything should be up and running. If you have any problems following this example, or want to leave feedback, feel free to open an issue on GitHub. 
If you have not already done, you should now install the connected JupyterHub Outpost services. Have a look at its documentation [here](https://jupyterhub-outpost.readthedocs.io/en/latest/usage/installation.html).
