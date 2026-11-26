# Portal User Guide

[简体中文](../../zh/Agent接入指导/Portal使用指南.md)

The Portal uses the memory instance your app already connects to. Once memory integration is set
up, open **Memory Management** from the app to view and manage personal memories available to the
current user.

## Open the Portal

Use the entry point for your integration to get a launch link for the current user. Request a new
link each time and pass the full URL, including the fragment after `#`, to a browser on the machine
serving the Portal. The link grants one-time access. Keep it out of logs and bookmarks.

### SDK or HTTP through a Provider

Reuse the `AgentMemoryMemoryProvider` already initialized with the app's identity and memory
scope. For example, a desktop app can attach this callback to its **Memory Management** menu.

```python
import webbrowser


async def open_memory_portal(provider):
    """Open a fresh Portal link in the desktop browser."""
    url = await provider.open_portal()
    if url is None:
        return False
    return webbrowser.open(url)
```

A web app opens the returned link in the current user's browser. If the host normally initializes
the Provider on the first conversation, complete the same initialization and scope binding before
offering the action. The host can use `provider.portal_status["supported"]` to decide whether to
show it.

Set page options with the Provider's `portal` constructor argument, for example
`portal={"language": "en"}`. When Portal is enabled, the SDK Provider attempts to start a local
page server during initialization. In HTTP mode, the Portal uses the memory service's existing port.

| Option | Usage |
| --- | --- |
| `enabled` | Controls the Provider's Portal action. Defaults to `True`. |
| `port` | Page port in SDK mode. The default, `0`, lets the system choose an available port. You can also set a fixed port. |
| `language` | Starting language for new links in SDK and HTTP modes. Use `en` for English or `zh` or `zh-CN` for Simplified Chinese. An omitted or blank value uses Simplified Chinese. |
| `instance_id` | Identifies an SDK document memory instance in a shared Portal. Defaults to `default`. Give each independent instance a distinct ID and reuse that ID when it restarts. |

SDK document memory instances under the same OS account share a Portal when they use the same
fixed local Markdown directory and read from the same local SQLite index. Use absolute paths for
both. On Windows, both must be on local NTFS storage. In a shared Portal, select the instance you
want to configure in **Model Settings**.

### Direct SDK Runtime integration

If your app uses `assemble_runtime()` directly, attach a Portal to the existing `runtime` once at
startup. Pass the host's trusted identity callback as `get_current_user_context`. When handling
memory or settings requests, the Portal calls it again and checks that the identity matches the
one bound at startup.

```python
from jiuwen_memory_entry.portal.hosting import start_portal

portal = start_portal(runtime, security_provider=get_current_user_context)
```

Call `portal.launch_url()` each time a user opens the page. When the host exits, call
`portal.close()` before closing the Runtime.

### Direct HTTP API calls

From the host, send `POST /portal/api/launch` to the running HTTP service using the current
user's memory service credentials. This example uses local port `8137` and a Bearer credential.
Set `MEMORY_ACCESS_TOKEN` in your terminal to the memory service credential. For local testing
with a fixed `dev` identity, you can omit the `Authorization` header.

```bash
portal_base_url=http://127.0.0.1:8137
curl -fsS "${portal_base_url}/portal/api/launch" \
  -H "Origin: ${portal_base_url}" \
  -H "Authorization: Bearer ${MEMORY_ACCESS_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{}'
```

Open the returned `portal_url` in a local browser. The HTTP Portal requires a loopback address and
the port the service listens on. Set `Origin` to the address used in the request. To bind writes to
an existing memory rail, pass its `space`, `agent`, and `session` in `write_target`. See the
[Portal API specification](../../specs/S02-memory-api.md#portal-页面操作与接口对应).

### Embed the Portal in the host's settings page

Get a fresh link for the current user and set the full URL as the iframe's `src`. The browser must
be able to reach the Portal at its loopback address. Hosting pages can embed the Portal without
configuring an origin allowlist. Remove any previous iframe origin settings when upgrading.
Request a new link each time you mount the iframe.

## View and manage memories

1. **Memory Overview** shows all records, active memories, correction history, forgotten and archived
   records, and the number of distinct source apps. **Daily additions** covers the last 365 days in
   your browser’s time zone and includes records in every status. **Memory sources** shows
   each source’s share of active memories, including records with an unknown source. Select a
   record count or a source in the chart to open its list.
2. **Memory Library** shows active memories by default. Use **Search by Text** to match content or
   tags and **Filters** to narrow the list by source app, Agent, project, or memory class. To ask
   a question in **All Memories**, switch to **Search by Meaning**, enter the question, set a
   source or result limit as needed, and select **Search**. Search by meaning only supports
   filtering by source app.
   The **User Profile**, **Project Knowledge**, and **Procedures** tabs open their
   corresponding views.
3. Select **Add Memory**, enter the content and an optional project, and select **Save Memory**.
   If the host has bound a memory rail, the new record uses that scope and its Agent is locked.
   If the Agent field is editable, leave it blank to save a personal memory.
4. Select **Correct** on an active record's row or in its details, edit the content, then preview
   and confirm the change. The previous version is available under **Correction History**. To remove a record
   from the active list and default search, select **Forget** and confirm. It remains visible
   under **Forgotten**.
5. Select individual records or use **Select This Page** to copy them or export Markdown or
   JSON. **Forget Selected** is available when every selected record is active. The Portal
   processes each record individually and reports any failures when it finishes.

Select **English** or **中文** in the top bar to switch the interface language. Current filters
and search results stay in place.

## Change model settings

1. Open **Model Settings**. If you see a **Configuration Instance** dropdown, select the
   instance you want to configure.
2. Edit the available model fields. When switching model services, enter the full URL, model
   name, and API key for the new service. When staying on the same service, leave the API key
   blank to keep the existing credential.
3. Select **Preview Changes**, review the changes, and select **Save and Apply**. Once the page
   confirms that the settings were saved and applied, new model calls use them. If it shows
   **Saved, pending application**, select **Retry Applying Saved Settings**.
4. To remove a saved override, select **Restore Startup Value** next to the field, preview the
   change, and confirm. The instance loads saved changes the next time it starts.

Editable settings depend on the host's permissions and configuration storage. The page marks
read-only fields and shows a message when configuration is unavailable.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| The page says the session has expired | Reopen it from the app or call the HTTP launch endpoint again. Use a fresh link each time. |
| The SDK cannot open the Portal | Check `portal_status["reason"]`. If a fixed port is in use, free it and try again, or set `portal.port` to `0` and restart. If shared instances have the same ID, assign a distinct ID to the conflicting instance and restart it. |
| The HTTP launch endpoint returns `404` or `401` | For `404`, check the loopback address, the service's actual port, and the `Origin` header. For `401`, check the access credentials. |
| An existing memory is missing from the list | Open **All Memories** in **Memory Library**, clear the search text and filters, and select the **All** status. Confirm that the record was written to the current memory instance and that the signed-in user has permission to view it. |
| Memories are visible and model settings are read-only | Have the host check that configuration management is enabled, the configuration store is writable, and the identity has permission to edit settings. HTTP enables configuration management automatically only for a declared fixed personal identity. A host can also configure its own storage and authorization. |

For more integration options, see the [Portal design](../../features/F05-personal-memory-portal.md#6-启动与打开页面).
