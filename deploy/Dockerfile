# syntax=docker/dockerfile:1
#
# Converge load balancer -- stock nginx plus one config file.
#
# Single stage, and that is the honest answer rather than an oversight: there is
# nothing to compile, so there is no build tooling to leave behind. A second
# stage would add ceremony and remove nothing. Multi-stage is a tool for
# separating build inputs from runtime outputs; this image has no build inputs.
#
# It is an image at all -- rather than the stock nginx image with the config
# bind-mounted from the host -- because a bind mount ties the stack to a path on
# this laptop. Stage 5 deploys this somewhere that has no /Users/... on it, and
# an image that carries its own config just runs.

ARG NGINX_VERSION=1.27
FROM nginx:${NGINX_VERSION}-alpine

# Which config to bake in. Two exist and they have different jobs:
#
#   nginx.docker.conf  local compose -- load-balances only, on :8080; the client
#                      is served separately on :5173
#   nginx.prod.conf    the public edge -- serves the client AND proxies the API
#                      on :80, so only one port is open to the internet
#
# A build ARG rather than two Dockerfiles, because everything else about the
# image is identical and a second Dockerfile would be a copy waiting to drift.
# A build ARG rather than a bind mount, because the image stays self-contained
# and therefore deployable somewhere that has no copy of this repo.
ARG NGINX_CONF=nginx.docker.conf

# Replaces the stock default.conf. See either config for why it has no http{}.
COPY ${NGINX_CONF} /etc/nginx/conf.d/default.conf

# Must match the `listen` directive in whichever config was baked in above:
# 8080 for the local load balancer, 80 for the public edge. Kept as an ENV as
# well as an ARG so the healthcheck below can read it -- a HEALTHCHECK cannot
# reference a build ARG, but its shell form expands environment variables at
# run time.
ARG NGINX_PORT=8080
ENV NGINX_PORT=${NGINX_PORT}

# EXPOSE is documentation plus a hint to tooling -- it publishes nothing on its
# own; compose's `ports:` does that.
EXPOSE ${NGINX_PORT}

# Fail the healthcheck unless nginx can actually reach a backend. Hitting
# /healthz proxies through to a server instance, so this checks the whole path
# -- nginx up, DNS resolving, an instance answering -- rather than just whether
# nginx accepted a socket. A load balancer that is "healthy" while every
# backend is down is worse than no healthcheck.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${NGINX_PORT}/healthz" || exit 1
