FROM debian:bookworm

ENV container=docker
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv dbus dpkg curl python3 ca-certificates adduser reprepro podman \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask systemd-remount-fs.service systemd-logind.service getty.target console-getty.service

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
