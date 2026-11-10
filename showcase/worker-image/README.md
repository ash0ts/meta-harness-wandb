# Pinned worker environment

`uv.lock` is the exact lock from the upstream example's local environment at continuation. `requirements.lock.txt` is exported with `uv export --frozen --offline --no-dev --no-emit-project --format requirements-txt`; all downloads are hash checked. The draft manifest records the requirements hash. Neither an amd64 image nor a wheelhouse is claimed to have been built or run by this continuation.

For prebuilt-image mode, build `Dockerfile` for linux/amd64, run the offline bundle with network disabled, publish the image to the chosen registry, and put its immutable digest in a new campaign manifest. Publishing an image is a separate external action.

For the recommended wheelhouse mode, download the locked **CPython 3.13 linux/amd64 wheels**, not the host Mac's wheels. Package only regular files at `wheels/*.whl`, `requirements.lock.txt` and `SHA256SUMS`. SHA256SUMS must list the requirements file and every wheel using paths relative to the archive root. Record the tar SHA-256, byte count, SHA256SUMS hash and requirements hash in a new manifest. The executor verifies all four before installation and installs with `--no-index --require-hashes`.

The existing public base-image digest is retained from the design. Live PF6 must prove architecture, Python version, package imports, offline tokenizer loading, egress denial, lifetime enforcement and cleanup. A local dependency lock alone does not establish these platform properties.
