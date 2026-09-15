FROM swebench/sweb.eval.x86_64.astropy_1776_astropy-8707@sha256:c6531e6e77ca7f257057f793cf38fc84427cd36fd93d136c21aef2a6e0e8df6d

USER root
COPY requirements.txt numpy-1.23.5-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl /workspace/dependency-install/
RUN mkdir -p /workspace/dependency-install/work \
    && TMPDIR=/workspace/dependency-install/work PIP_CONFIG_FILE=/dev/null \
       /opt/miniconda3/envs/testbed/bin/python -m pip install \
       --no-index --no-deps --no-cache-dir --no-compile --require-hashes \
       --find-links=/workspace/dependency-install -r /workspace/dependency-install/requirements.txt \
    && /opt/miniconda3/envs/testbed/bin/python -m pip check \
    && rm -rf /workspace/dependency-install
