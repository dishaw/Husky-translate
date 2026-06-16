# Layout-Preserving Document Translator (LLM-Powered)

An advanced, layout-preserving document translation platform powered by Large Language Models (LLMs). It splits Word, PowerPoint, PDF, and image files into manageable segments, translates them via configured LLM APIs, and reconstructs the output files while attempting to preserve original formats and layouts as closely as possible.

---

### 📌 Important Notice: Architecture & Runtime Environment

> **Read this first before downloading:** 
> This project is architected as an **Odoo 18 Module** (leveraging Odoo's robust ORM, HTTP JSON Controllers, and the OWL frontend framework). 
> 
> * **For Enterprise Users / Integrators:** It integrates seamlessly into your existing Odoo 18 ERP workflow as a professional translation workbench.
> * **For Students & Developers:** It serves as a comprehensive, real-world codebase for learning modern **Odoo 18 module development**, **OWL (Odoo Web Library) components**, **asynchronous LLM integrations**, and **advanced Python document XML parsing**. *It cannot run as a standalone CLI script without an Odoo environment.*

---

## ✨ Key Features

### 📐 1. High-Fidelity Layout Preservation
* **Word (DOCX):** Reconstructs translations directly inside the original DOCX XML structure, aiming to preserve original templates, styles, custom tables, images, and headers/footers.
* **PowerPoint (PPTX):** Replaces texts inside existing PPTX shapes and text boundaries to minimize layout overflow.
* **PDF & Images (OCR):** Extracts text coordinates via multimodal/vision LLMs and overlays translated texts precisely back onto the original coordinates.

### 📝 2. Bilingual Preview & Custom Export
* **Split View:** Side-by-side (left: original, right: translated) interactive workspace.
* **Bilingual View:** Alternating vertical display of source and target segments.
* **Flexible Exports:** 
  * Export pure translated files.
  * Export **Bilingual DOCX** with alternating source/translated paragraphs. Tables are exported using an alternating row-by-row layout for easier verification.

### 🖼️ 3. Multimodal OCR & Interactive Overlay Editing
* Detects text blocks on image-based PDFs and graphics using vision-capable LLMs.
* Generates an editable translation overlay layer. Users can drag text boxes, adjust font sizes, modify translations, or delete text boxes directly on the canvas.

### 🧠 4. Glossary & Active Learning
* Integrates a dedicated glossary system to ensure consistent translations for proprietary terms and technical vocabulary.
* **Active Learning:** Automatically learns and refines glossary entries from manual revisions made by users during editing.

### ⚙️ 5. Smart Batching & Failure Retries
* Intelligently splits long paragraphs by sentences to avoid exceeding LLM context limits.
* Automatically skips blank lines and directly copies non-translatable tokens (such as pure numbers, units, and serial numbers) to save token usage.
* Pauses with an alert if 5 consecutive translation errors occur, preventing continuous API credit loss while preserving already completed work.

---

## 🛠️ Technology Stack

* **Platform:** Odoo 18 Addon
* **Backend Framework:** Odoo ORM, Odoo HTTP JSON Controller
* **Frontend Framework:** Odoo Web Client Action, OWL (Odoo Web Library) Component
* **Dependencies & Modules:**
  * **Core LLM Capabilities:** Relies on the host system's `llm`, `llm_thread`, `llm_tool`, and `llm_assistant` modules.
  * **Glossary Management:** Relies on `llm_knowledge`.
  * **Project Management:** Relies on Odoo `project` module.
* **Libraries Used:**
  * **Word Parsing:** `python-docx` (combined with raw XML manipulation)
  * **PPTX Parsing:** `python-pptx`
  * **PDF Parsing:** `PyMuPDF` (imported as `fitz`)
  * **Legacy Conversions:** Headless LibreOffice integration (converts older `.doc` / `.ppt` to modern XML formats).

---

## 📂 Supported File Formats

* **Documents:** Microsoft Word (`.docx`, `.doc`), PowerPoint (`.pptx`, `.ppt`)
* **PDFs:** Vector PDFs and Scanned/Image PDFs (`.pdf`)
* **Images:** `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`, `.tiff`, `.tif`, `.svg`

*Note: OCR and PDF image translation require selecting a vision-enabled/multimodal LLM (such as GPT-4o, Claude 3.5 Sonnet, etc.).*

---

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have a running **Odoo 18** instance and a server equipped with Python dependencies:
```bash
pip install python-docx python-pptx PyMuPDF
```
*(Optional) If you need legacy `.doc` and `.ppt` support, install LibreOffice on your server host:*
```bash
# Ubuntu/Debian example
sudo apt-get install libreoffice
```

### 2. Addon Installation
1. Clone this repository into your Odoo custom addons directory.
2. Ensure dependency modules (`llm`, `llm_knowledge`, `project`, etc.) are present in your addons path.
3. Log in to Odoo with Administrator privileges, activate **Developer Mode**.
4. Navigate to **Apps > Update Apps List**, search for `llm_translate` (or `Layout-Preserving Document Translator`), and click **Activate**.

### 3. Usage Workflow
1. Go to **LLM > Translation** in the left menu to open the translation workbench.
2. Select your project, language pairs, preferred LLM Provider, and Model.
3. Upload your document and wait for the extraction process to finish.
4. Click **Start/Translate** to execute segment-by-segment translation.
5. Review, edit translation errors, or adjust image text boxes dynamically.
6. Export your final translated or bilingual document.

---

## 💡 Troubleshooting & FAQ

#### Q: Why is my DOCX/PPTX format slightly shifted after export?
While our system utilizes structural XML reconstruction to keep styling intact, highly complex templates (with floating text boxes, nested merged cells, or sophisticated custom bullet numbering) may require minor manual adjustments post-export.

#### Q: How do I resolve "OCR Failed" errors on image files?
Verify that your selected LLM Provider and Model support **multimodal / vision inputs**. Pure text models cannot process graphical files.

#### Q: Can I resume translations if my network drops?
Yes. Simply reopen the task and click **Start/Translate** again. Already completed segments are saved in the Odoo database and will not be re-translated.

---

## 🤝 Contributing & Academic Use

This project welcomes contributions from both community developers and students. 
* **For Students:** If you are using this codebase as a reference for your academic research, course project, or graduation thesis, please feel free to fork, explore, and cite our repository.
* **For Issues & PRs:** If you find bugs or have feature improvements (especially regarding XML document reconstruction or OWL frontend UI), please open an Issue or submit a Pull Request.

---

## 📄 License

This project is licensed under the LGPL-3 License.




test-ldc