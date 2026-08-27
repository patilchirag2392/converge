# syntax=docker/dockerfile:1
#
# Converge client -- React + Vite, two-stage build.
#
#   builder   Node, node_modules, tsc, Vite -- produces /app/dist
#   runtime   nginx serving /app/dist as static files
#
# This is the clearest case for a multi-stage build in the whole project,
# because the two stages do not even share a *language runtime*. The output of
# `vite build` is HTML, JS and CSS: once it exists, Node has no further job.
# Shipping the builder would mean shipping a Node runtime and a node_modules
# tree (hundreds of MB, and the source of most of the CVE noise in a typical
# frontend image) to serve files that nginx serves from disk. See NOTES.md.

ARG NODE_VERSION=22
ARG NGINX_VERSION=1.27

# ---------------------------------------------------------------- builder ----
FROM node:${NODE_VERSION}-alpine AS builder

WORKDIR /app

# Manifest + lock first, before the source, so the (slow) install layer is
# cached and only re-runs when the dependencies actually change.
COPY package.json package-lock.json ./

# `npm ci` rather than `npm install`: it installs exactly package-lock.json and
# errors if the lock disagrees with package.json, instead of quietly updating
# the lock mid-build. A build that can change its own inputs is not reproducible.
RUN npm ci

COPY . .

# Vite inlines `import.meta.env.VITE_*` into the bundle at BUILD time -- there is
# no runtime lookup left in the output, because the browser has no environment
# to read. So the WebSocket URL has to arrive as a build ARG, not as a compose
# `environment:` entry.
#
# This is the one place Stage 4's "config via environment variables" rule bends,
# and it bends because of how the bundler works, not because of a shortcut. The
# tradeoff, and the runtime-injection alternative, are written up in NOTES.md.
#
# Default targets the load balancer, which is the whole point of the stage: the
# browser talks to nginx on :8080, not to any single instance.
ARG VITE_WS_URL=ws://localhost:8080
ENV VITE_WS_URL=${VITE_WS_URL}

# `npm run build` is `tsc -b && vite build` -- so a type error fails the image
# build. That is deliberate: it makes `docker build` a real check in CI rather
# than just a packaging step.
RUN npm run build

# ---------------------------------------------------------------- runtime ----
FROM nginx:${NGINX_VERSION}-alpine AS runtime

# Replaces the stock default.conf. conf.d/*.conf is included from inside the
# image's http{} block, so this file needs only the server{} -- no duplicated
# worker/events/http boilerplate to drift out of sync with the base image.
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# wget, not curl: busybox in nginx:alpine already provides it, so this costs
# nothing. -q -O /dev/null makes it a pure exit-status check.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

# The base image's own CMD (`nginx -g 'daemon off;'`) is correct and inherited:
# nginx must stay in the foreground or the container would exit the moment it
# daemonised.
