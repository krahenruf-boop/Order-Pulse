class NoNetworkWise:
    """Existing tests never make real Wise requests."""
    def fetch(self):
        raise RuntimeError('FAKE_WISE_OFFLINE')
