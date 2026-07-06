# 微信公众号文章采集

从飞书多维表格读取待采集的公众号文章链接，自动抓取标题、正文、阅读量等数据后回写至飞书。

## 工作流程

```
飞书多维表格（待采集链接）→ 脚本采集 → 回写飞书
                                ↓
                        标题 / 正文 / 摘要
                        阅读量 / 在看数 / 发布时间
```

## 项目结构

```
├── main.py                        # 主采集脚本
├── requirements.txt               # Python 依赖
└── .github/workflows/collect.yml  # GitHub Actions 定时任务
```

## 飞书表格字段

| 字段名 | 说明 |
|---|---|
| 公众号名称 | 主键，工具写入 |
| 标题 | 文章标题，脚本回填 |
| 发布时间 | 脚本回填 |
| 原文链接 | 工具写入 |
| 采集状态 | 待采集 → 已采集 / 采集失败 / 链接失效 |
| 采集时间 | 脚本回填 |
| 正文摘要 | 前 200 字 |
| 正文内容 | 完整正文 |
| 阅读量 | 需微信 Cookie |
| 在看数 | 需微信 Cookie |

## 首次配置

### 1. GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 值 |
|---|---|
| `FEISHU_APP_ID` | `cli_a938e7232e38dbdf` |
| `FEISHU_APP_SECRET` | `XA8AloYl8LV1syReyBSZn0wBWUbNbDeZ` |
| `FEISHU_APP_TOKEN` | `R0f5baQPWa1b3bsyOyfcQTiqnSg` |
| `FEISHU_TABLE_ID` | `tblsKvluKnlv3uqY` |
| `WECHAT_COOKIE` | 微信文章页的 Cookie（可选，没有则阅读量/在看数取不到） |

### 2. 飞书应用权限

已开通：`bitable:app`

### 3. 获取微信 Cookie（可选）

1. 浏览器打开一篇公众号文章
2. F12 → Application → Cookies → 复制完整的 Cookie 字符串
3. 填入 GitHub Secrets → `WECHAT_COOKIE`

> Cookie 有效期通常几天到几周，过期后阅读量和在看数会取不到，但不影响标题正文采集。届时重新获取并更新 Secret 即可。

## 运行方式

- **自动**：每周五 15:00（北京时间）定时执行
- **手动**：GitHub Actions → 公众号文章采集 → Run workflow
