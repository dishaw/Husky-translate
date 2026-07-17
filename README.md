# HUSKY TRANSLATE — 智能文档翻译平台

基于 **Cloudreve 文档管理 + OnlyOffice Document Server + LLM AI 插件** 的轻量级文档翻译平台。选中原文 → AI 翻译 → 保留格式写回，零学习成本。

---

## 架构概览

| 组件 | 技术 | 端口 | 说明 |
|------|------|------|------|
| 前端门户 | nginx:alpine | 8070 | 纯静态品牌 landing page（`husky.html`） |
| 文档管理 | Cloudreve (Go) | 8080 | 文件夹树 / 拖拽上传 / 权限 / 分享 / 检索 |
| Document Server | OnlyOffice | 8090 | 文档编辑与渲染引擎（插件定制保留） |

---

## 快速启动

```powershell
# 构建并启动所有服务
docker compose up -d --build

# 查看状态
docker compose ps

# 查看日志
docker compose logs --tail 40 portal
docker compose logs --tail 40 onlyoffice
```

启动后访问：
- **前端门户**：http://localhost:8070 → 自动跳转 `husky.html`
- **Cloudreve 管理**：http://localhost:8080
- **OnlyOffice**：http://localhost:8090

---

## 核心功能

### 1. 文档翻译（保留格式）
- 在 OnlyOffice 中打开文档，选中文本，通过 **Husky 翻译插件** 一键翻译
- 翻译使用 AI 插件中配置的 LLM 模型（支持 OpenAI / 自定义 Provider）
- 译文通过 `ReplaceTextSmart` 写回，**保留原文段落结构、字体、颜色、样式**
- 支持段落数自动适配：译文段落数与原文选中段落数对齐

### 2. 插件体系

| 插件 | GUID | 类型 | 功能 |
|------|------|------|------|
| **Husky 翻译** | `{F30B...1B5}` | 面板 | 选中翻译 UI + 语言选择 + 自动翻译开关 |
| **AI 引擎** | `{9DC9...DD007}` | 后台 | LLM 模型管理 + 翻译/Chat/摘要/OCR/生图 |
| **Google 翻译** | `{7327...6800}` | 窗口 | Google Translate 内嵌（备选方案） |

### 3. 通信机制
- **Husky → AI**：`localStorage` 写入 `husky_selection_text`，AI 插件每 800ms 轮询
- **AI → 面板**：`localStorage` 写入 `husky_selection_result`，面板轮询展示
- **语言设置**：`husky_target_language` / `onlyoffice_ai_plugin_translate_lang`
- **自动翻译**：`husky_auto_translate`（1 = 选中即翻，0 = 手动触发）



---

## 文件结构

```
husky-trans/
├── docker-compose.yml          # 三服务编排
├── Dockerfile                  # nginx 门户镜像
├── nginx.conf                  # 门户 nginx 配置（含 /callback/ 和 /uploads/）
├── husky.html                  # 前端品牌首页
├── 开发宪法.md                  # 开发规范与约定
│
├── onlyoffice_plugins/         # OnlyOffice 插件（volume 挂载）
│   ├── {F30B...1B5}/           # Husky 翻译插件
│   ├── {9DC9...DD007}/         # AI 引擎插件（LLM 模型管理 + 翻译轮询）
│   ├── {7327...6800}/          # Google 翻译插件
│   └── ...                     # 其他辅助插件
│
├── cloudreve_data/             # Cloudreve 持久化数据（配置 + SQLite + 上传）
├── onlyoffice_plugins/         # OnlyOffice 插件目录
└── uploads/                    # 门户上传临时目录
```

---

## 常见操作

```powershell
# 重启前端门户
docker compose restart portal

# 重启 OnlyOffice（插件修改后）
docker compose restart onlyoffice

# 查看 OnlyOffice 日志
docker compose logs --tail 100 onlyoffice

# 删除 .gz 缓存（插件 JS 修改后必须执行）
Remove-Item -Recurse -Force onlyoffice_plugins/*/scripts/*.gz
Remove-Item -Recurse -Force onlyoffice_plugins/*/*.gz

# 完全重建
docker compose down
docker compose up -d --build
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | 原生 HTML/CSS/JS，零框架 |
| 反向代理 | nginx:alpine |
| 文档管理 | Cloudreve (Go + SQLite) |
| 文档引擎 | OnlyOffice Document Server |
| AI 集成 | OnlyOffice AI Plugin（支持 OpenAI 兼容 API） |
| 容器化 | Docker Compose |

---

## License

LGPL-3.0
