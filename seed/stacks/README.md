# stacks/

One folder per app: `stacks/<id>/compose.yaml`, plus `ingress.yaml` when it has a web address.
Create them with `./homelab.py new-app` or the **New app** tab of homegraf (both add the folder, the inventory entry, the Komodo
stack, the secrets and open a pull request); merging the pull request deploys the app.
