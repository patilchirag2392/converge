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

# Replaces the stock default.conf. See the file for why it has no http{} block.
COPY nginx.docker.conf /etc/nginx/conf.d/default.conf

# Matches the `listen 8080` in the config. EXPOSE is documentation plus a hint
# to tooling -- it publishes nothing on its own; compose's `ports:` does that.
EXPOSE 8080

# Fail the healthcheck unless nginx can actually reach a backend. Hitting
# /healthz proxies through to a server instance, so this checks the whole path
# -- nginx up, DNS resolving, an instance answering -- rather than just whether
# nginx accepted a socket. A load balancer that is "healthy" while every
# backend is down is worse than no healthcheck.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
