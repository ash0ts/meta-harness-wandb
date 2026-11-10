# SPDX-License-Identifier: MIT
# Build with: docker build --platform linux/amd64 -t mhforge-worker:local .
FROM public.ecr.aws/docker/library/python@sha256:f3dac913ab53b78c64f26d5518fcd7043d5a1a46522d6a493b598c3ed12f62a6
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt
WORKDIR /mhf
# Credentials and experiment data arrive only through the runtime's separate channels.
CMD ["sleep", "infinity"]
