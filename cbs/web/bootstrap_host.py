"""Only config/client/store are needed before the first partner is published."""
class BootstrapHost:
    def __init__(self, config, store, client):
        self._config, self.store, self._client = config, store, client
        from cbs.costs import bind
        bind(client, store, config)
        self.active = None

    @property
    def config(self):
        return self.active.config if self.active else self._config

    @config.setter
    def config(self, value):
        self._config = value

    @property
    def client(self):
        return self.active.client if self.active else self._client

    @client.setter
    def client(self, value):
        self._client = value
