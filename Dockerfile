# panda-grafana-adapter
#
# The adapter shells out to the `panda` CLI, which must be on PATH and
# authenticated (it reads ~/.config/panda). Running ON THE HOST is the
# recommended mode (see README) since that's where panda is authed.
#
# To containerize, mount a *Linux* panda binary + your config/credentials, and
# give the container access to the host (for host.docker.internal:2480 panda-server):
#
#   docker run --rm -p 9119:9119 \
#     -v /path/to/linux/panda:/usr/local/bin/panda:ro \
#     -v $HOME/.config/panda:/root/.config/panda \
#     --add-host host.docker.internal:host-gateway \
#     panda-grafana-adapter
#
FROM python:3.12-slim
WORKDIR /app
COPY panda_grafana_adapter.py /app/
EXPOSE 9119
ENTRYPOINT ["python3", "panda_grafana_adapter.py", "--bind", "0.0.0.0", "--port", "9119"]
