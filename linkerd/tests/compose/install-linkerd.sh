set -euo pipefail

# Use config mapped to /root/.kube/config
kubectl config set-context kind-linkerd

# Install linkerd CLI and deploy
echo "###  GATEWAY API INSTALL  ###"
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.1/standard-install.yaml
echo "###  LINKERD CRDS INSTALL  ###"
linkerd install --crds | kubectl apply -f -
echo "###  LINKERD INSTALL  ###"
linkerd install | kubectl apply -f -
echo "###  LINKERD CHECK  ###"
linkerd check # will wait for linkerd to be available

# Install demo linkerd app
echo "###  EMOJIVOTO DEPLOY  ###"
curl -fsSL https://run.linkerd.io/emojivoto.yml | kubectl apply -f -
echo "###  EMOJIVOTO WAIT  ###"
kubectl wait pods -n emojivoto --all --for=condition=Ready --timeout=300s
echo "###  EMOJIVOTO INJECT  ###"
kubectl get -n emojivoto deploy -o yaml | linkerd inject - | kubectl apply -f -
echo "###  EMOJIVOTO CHECK  ###"
linkerd -n emojivoto check --proxy
echo "###  LINKERD METRICS SERVICE  ###"
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: linkerd-proxy-metrics
  namespace: emojivoto
spec:
  selector:
    app: web-svc
  ports:
  - name: proxy-metrics
    port: 4191
    targetPort: 4191
EOF
for attempt in $(seq 1 30); do
    if [ "$(kubectl -n emojivoto get endpointslices -l kubernetes.io/service-name=linkerd-proxy-metrics -o jsonpath='{.items[0].endpoints[0].addresses[0]}')" ]; then
        break
    fi
    sleep 1
done
test -n "$(kubectl -n emojivoto get endpointslices -l kubernetes.io/service-name=linkerd-proxy-metrics -o jsonpath='{.items[0].endpoints[0].addresses[0]}')"
echo "###  LINKERD DEPLOY COMPLETE  ###"

# run forever so container doesn't exit
tail -f /dev/null
