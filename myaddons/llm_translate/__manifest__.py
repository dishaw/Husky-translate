{
    "name": "LLM Translation",
    "summary": "AI-powered document translation with knowledge base glossary support",
    "description": """
LLM Translation for Odoo
=========================
Translate documents and images using AI models with knowledge base
glossary integration. Features include:

- Upload and translate Word documents (doc/docx), PowerPoint presentations (ppt/pptx), PDFs, and images (jpg/png/bmp/gif/webp)
- OCR-based image translation with draggable, resizable text overlays
- Side-by-side original/translated view
- Paragraph-by-paragraph translation to avoid token limits
- Auto-split long paragraphs by sentence (>2000 tokens)
- Knowledge base glossary lookup for terminology consistency
- Save source and translated files to Odoo projects
- Select specific LLM provider/model for translation
- Resume interrupted translations
    """,
    "category": "Productivity",
    "version": "18.0.1.0.0",
    "depends": [
        "base",
        "mail",
        "web",
        "llm",
        "llm_thread",
        "llm_tool",
        "llm_assistant",
        "llm_knowledge",
        "project",
    ],
    "external_dependencies": {
        "python": ["docx"],
    },
    "author": "Custom",
    "data": [
        "security/llm_translate_security.xml",
        "security/ir.model.access.csv",
        "views/llm_translation_views.xml",
        "views/llm_translate_menu.xml",
        "data/translation_home_action.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "llm_translate/static/src/components/llm_translation_view/llm_translation_view.js",
            "llm_translate/static/src/components/llm_translation_view/llm_translation_view.xml",
            "llm_translate/static/src/components/llm_translation_view/llm_translation_view.scss",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
    "auto_install": False,
}
