# Installation

In this section a simple Outpost Installation for an existing JupyterHub OutpostSpawner is described.

To install an Outpost for JupyterHub, one public key is required for each connected JupyterHub. After the installation, each JupyterHub must know the defined username / password combination to configure the OutpostSpawner correctly.

## Requirements
 - One k8s cluster
 - [Helm](https://helm.sh/) CLI
 - [kubectl](https://kubernetes.io/de/docs/reference/kubectl/) CLI
 - One public key from each connected JupyterHub

## Installation

For the Outpost instance two secrets are required:
 - An encryption key for the database. When starting a single-user server, Outpost will encrypt the given data and store it in a database.  
 - Usernames / Passwords for authentication of the connected JupyterHubs. Multiple values must be separated by a semicolon.  

```
# Create secret for encryption key
pip install cryptography
SECRET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
kubectl create secret generic outpost-cryptkey --from-literal=secret_key=${SECRET_KEY}

# Create secret for usernames / passwords
JUPYTERHUB_PASSWORD=$(uuidgen)
kubectl create secret generic --from-literal=usernames=jupyterhub --from-literal=passwords=${JUPYTERHUB_PASSWORD} outpost-users
```

Installation with Helm:
```
cat <<EOF >> values.yaml
cryptSecret: outpost-cryptkey
outpostUsers: outpost-users
sshPublicKeys:
  - <enter the SSH public key from JupyterHub here>
EOF

helm repo add jupyterhub-outpost https://kreuzert.github.io/jupyterhub-outpost/charts/
helm repo update
helm upgrade --install -f values.yaml outpost jupyterhub-outpost/jupyterhub-outpost
```

## Make Outpost reachable for JupyterHub

JupyterHub will connect the Outpost on 2 ports:
 - API Endpoint to start/poll/stop the single-user server
 - SSH to enable port forwarding
  
For the first one it's recommended to use an ingress class with encryption. The second one can be of type LoadBalancer or NodePort.
If JupyterHub and Outpost are running in the same k8s cluster, ClusterIP for both services should be fine.

```
cat <<EOF >> secure_values.yaml
cryptSecret: outpost-cryptkey
outpostUsers: outpost-users
sshPublicKeys:
  - <enter the SSH public key from JupyterHub here>
servicessh:
  type: LoadBalancer
  loadBalancerIP: <add your floating ip for the ssh connection in here>
ingress:
  enabled: true
  annotations: # used for Let's Encrypt certificate
    acme.cert-manager.io/http01-edit-in-place: "false"
    cert-manager.io/cluster-issuer: letsencrypt-cluster-issuer
  hosts:
    - <your hostname to reach the API Endpoint>
  tls:
  - hosts:
    - <your hostname to reach the API Endpoint>
    secretName: outpost-tls
EOF

helm repo add jupyterhub-outpost https://kreuzert.github.io/jupyterhub-outpost/charts/
helm repo update
helm upgrade --install -f secure_values.yaml outpost jupyterhub-outpost/jupyterhub-outpost
```
