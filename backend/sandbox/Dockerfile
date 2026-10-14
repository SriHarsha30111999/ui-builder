# Build sandbox image for generated React apps. Dependencies are installed HERE (with network, from this trusted
# manifest) so that the sandboxed build itself runs with no network and never installs anything.
#
#   docker build -t screenshot-builder-sandbox:1 backend/sandbox
FROM node:22-slim
WORKDIR /opt/toolchain
COPY package.json ./
RUN npm install --no-audit --no-fund --ignore-scripts && \
    mkdir -p /work && chown 1000:1000 /work
# Resolve tsc / vite / react from the filesystem root so /work needs no node_modules.
RUN ln -s /opt/toolchain/node_modules /node_modules && ln -s /opt/toolchain/node_modules/.bin/tsc /usr/local/bin/tsc \
    && ln -s /opt/toolchain/node_modules/.bin/vite /usr/local/bin/vite
USER 1000:1000
WORKDIR /work
