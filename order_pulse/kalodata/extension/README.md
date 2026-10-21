# Existing Kalodata extension source

This is the existing TikTok OPS 素材采集 MV3 extension, retained here so its
fixes and backend version requirements can be maintained together. No new
collector, extension identity, login, or browser profile is introduced.

Current: **1.1.8 / 20261021.1**, protocol 2, adapter 2026-09-v2.

Existing product tabs navigate to the host's cleaned URL before extraction,
discarding old date and category parameters even when the same product is open.
Readiness checks the committed document URL as well as DOM readiness, so a
previous document or a Chromium network-error page cannot count as loaded.

Explicit extraction refreshes its product tab and reads details in inactive tabs.
It never activates a source tab or navigates the Order Pulse workbench.
The adapter reveals the video section heading after the date refresh and before
reading pagination. Changing dates resets the scroll position and lazy count;
revealing it earlier can leave the provisional one-page pager unchanged.
Kalodata defers its count request until that heading enters the viewport.
The host keeps only the bound Space's Kalodata tabs rendering during a task,
then removes the rendering lease on success, failure, cancellation or shutdown.

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

Explicit extraction refreshes its product tab once in the background. Recognized
empty-result text is accepted without a pagination bar; unknown missing
pagination still fails closed. The host obtains the saved Space control and
shared operation mutex before dispatching work; passive health never claims
control.
