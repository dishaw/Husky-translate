# odoo-trans

Clean Odoo 18 environment for LLM Translation.

Included custom modules:

- `llm_translate`
- `llm`
- `llm_thread`
- `llm_tool`
- `llm_assistant`
- `llm_knowledge`
- `llm_store`
- `web_json_editor`

Start:

```powershell
cd D:\odoo-trans
docker compose up -d --build
```

By default the build uses the local base image `odoo-translator:v1` to avoid pulling `odoo:18.0` through a broken mirror. Override it when needed:

```powershell
$env:ODOO_BASE_IMAGE = "odoo:18.0"
docker compose up -d --build
```

Open:

```text
http://localhost:8070
```

Install the app/module `LLM Translation` (`llm_translate`) in Odoo. Odoo will install its dependency modules automatically.

Provider modules such as `llm_openai` or `llm_ollama` are intentionally not included because they are optional, not manifest dependencies of `llm_translate`.
