FROM bash:5.2.15
ENV LINKERD2_VERSION=edge-26.6.3
ENV LINKERD_AMD64_SHA256=b7f9ad23cae615cad930eb31aa6e733e3fa85b5f6e9e0229600a4e1cfcd5cfca
ENV LINKERD_ARM64_SHA256=803aa3ecdb3978a95f9a16a411e35976d0fcbe67404be485af6a0d62c4affe20
ENV KUBECTL_VERSION=v1.35.5
ENV KUBECTL_AMD64_SHA256=90f75ea6ecc9ea5633262e1c0b83a40560003b30fc94a04cb099404fcef0c224
ENV KUBECTL_ARM64_SHA256=ac69e06fd6860d69786692f5af1c3a1208ed54f8366a4d97ab15c172e99765ee
RUN set -eux; \
    apk --no-cache add dumb-init gettext ca-certificates openssl curl; \
    kubectl_arch="$(apk --print-arch)"; \
    case "$kubectl_arch" in \
        x86_64) kubectl_arch="amd64"; kubectl_sha256="$KUBECTL_AMD64_SHA256"; linkerd_sha256="$LINKERD_AMD64_SHA256" ;; \
        aarch64) kubectl_arch="arm64"; kubectl_sha256="$KUBECTL_ARM64_SHA256"; linkerd_sha256="$LINKERD_ARM64_SHA256" ;; \
        *) echo "Unsupported architecture: $kubectl_arch"; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl"; \
    echo "$kubectl_sha256  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod +x /usr/local/bin/kubectl; \
    curl -fsSLo /usr/local/bin/linkerd "https://github.com/linkerd/linkerd2/releases/download/${LINKERD2_VERSION}/linkerd2-cli-${LINKERD2_VERSION}-linux-${kubectl_arch}"; \
    echo "$linkerd_sha256  /usr/local/bin/linkerd" | sha256sum -c -; \
    chmod +x /usr/local/bin/linkerd

WORKDIR /
COPY install-linkerd.sh /
RUN chmod +x install-linkerd.sh

ENTRYPOINT /usr/local/bin/bash /install-linkerd.sh
