# Existing Kalodata extension source

This is the existing TikTok OPS 素材采集 MV3 extension, retained here so its
fixes and backend version requirements can be maintained together. No new
collector, extension identity, login, or browser profile is introduced.

Current: **1.1.5 / 20261015.1**, protocol 2, adapter 2026-09-v2.

For the existing personal Mac deployment, copy only the changed source files
into its original unpacked-extension folder shown in the browser's extension
Details, then reload that same extension. Keep its ID, pairing and storage;
do not remove it or load a second copy. The backend rejects older versions
with PLUGIN_RELOAD_REQUIRED before handing them work.

Refresh regression: changing a preset may select the button before the old
video list begins loading. Wait for visible loading or a changed list, then
stable rows, pagination and sorting. If the date change is declined but a
stable list remains, dates are advisory. A loading timeout or real identity
change still stops collection.

Local targeted tests:

    node tests/kalodata_refresh_contract.mjs
    .venv/bin/python -m unittest tests.test_kalodata_refresh

No credentials, pairing tokens, browser data or database files belong here.

Explicit extraction now activates and refreshes its product tab once before reading, preventing dormant-page timers and stale failed lists. Recognized empty-result text is accepted without a pagination bar; unknown missing pagination still fails closed. Plugin response timeouts have a separate error from network failures. The host obtains the saved Space control and shared operation mutex before dispatching work; passive health never claims control.
