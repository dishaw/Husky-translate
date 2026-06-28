# Cloudreve + ONLYOFFICE 集成配置指南

## 📌 当前状态

✅ Cloudreve 已成功启动！所有服务运行正常：

| 端口 | 服务 | 用途 |
|------|------|------|
| **8070** | nginx 门户 | HUSKY 品牌 landing page（`husky.html`） |
| **8080** | Cloudreve | 📁 文档管理后台（上传/管理/分享） |
| **8090** | ONLYOFFICE | ✏️ 文档编辑器（你定制的插件全在） |

---

## 🔑 管理员账号

- **URL**: http://127.0.0.1:8080
- **邮箱**: `admin@husky-translate.com`
- **密码**: `HuskyTranslate2026!`

> ⚠️ 首次登录后请立即修改密码！

---

## 🔗 第三步：连接你已定制的 ONLYOFFICE

1. 登录 Cloudreve → 点击左侧 **「管理面板」**
2. 进入 **「参数设置」** → **「文档预览与编辑」**
3. 找到 **「WOPI 客户端地址」**（即 ONLYOFFICE Document Server 地址）
4. 填入：
```
http://127.0.0.1:8090/
```
5. 点击保存

> ⚠️ **为什么填 `127.0.0.1` 而不是 `onlyoffice`（Docker 内部 DNS）？**
>
> 因为 ONLYOFFICE 编辑器是在**你的浏览器**中加载的，浏览器需要能直接访问这个地址。`127.0.0.1:8090` 对你本机浏览器可达。

---

## 🏷️ 品牌定制

在 Cloudreve 管理面板中：

1. **参数设置 → 站点信息**：
   - 站点名称：`HUSKY TRANSLATE`
   - 站点描述：`长久并肩，信赖相托 · Your Trusted Language Companion`
   - 上传自定义 Logo

2. **用户管理**：
   - 创建译员、客户等不同角色
   - 设置存储空间配额
   - 配置文件权限

3. **存储策略**：
   - 默认使用本地存储
   - 后续可扩展 MinIO / S3 / OSS

---

## 🔄 日常运维命令

```powershell
# 全部启动
docker compose up -d

# 仅重启 Cloudreve
docker compose restart cloudreve

# 查看所有容器状态
docker compose ps

# 查看 Cloudreve 日志
docker compose logs --tail 50 cloudreve

# 备份数据（重要！）
# Cloudreve 所有数据在 ./cloudreve_data/ 目录
# 包含：SQLite 数据库 + 上传文件 + 配置文件
```

---

## ✅ 验证一切正常

1. 在 Cloudreve 中点击 **「新建」** → 上传一个 `.docx` 文件
2. **双击**该文件
3. 文档在 ONLYOFFICE 中打开后，检查顶部菜单栏 → **「插件」** 选项卡
4. 你之前定制的所有 AI 翻译插件应全部可见可用

---

## 📊 对比：Cloudreve vs KodBox

| 特性 | Cloudreve | KodBox |
|------|-----------|--------|
| 语言 | Go（编译型，更快） | PHP（解释型） |
| 内存占用 | ~20MB | ~30-50MB |
| 镜像大小 | ~530MB（含编译工具） | ~200MB |
| ONLYOFFICE | ✅ 原生支持 | ✅ 插件支持 |
| 存储后端 | 20+ (本地/S3/OSS等) | 本地为主 |
| WebDAV | ✅ | ✅ |
| 分享链接 | ✅ | ✅ |
| 离线下载 | ✅ | ❌ |
| 中国维护团队 | ✅ | ✅ |

> KodBox 的 `kodcloud/kodbox` 镜像因 Docker Desktop 镜像源限制无法拉取。Cloudreve 可以直接拉取并已成功运行。
