# 电子教材数字图书馆

基于 [ancient-books](https://github.com/rockzhang99/ancient-books) 技术方案，从 [国家中小学智慧教育平台](https://basic.smartedu.cn/tchMaterial) 采集电子教材资源，构建本地可搜索、可阅读的教材数字图书馆。

> 目前已收录 **2637 本教材**，覆盖小学、初中、高中、特殊教育等 6 个学段，37 个学科。其中 **1906 本已下载完成**，731 本因源站未公开图片资源而无法下载（详见[下载状态说明](#下载状态说明)）。

## 功能特性

- **元数据采集** — 直接请求 CDN 分片 JSON，无需浏览器模拟，秒级获取全量教材元数据
- **页面图片下载** — 批量下载教材高清页面图片（每页 ~800KB，1437×1006 分辨率），支持断点续传
- **在线搜索** — 按书名、学科、出版社模糊搜索
- **分类筛选** — 学段 → 学科 → 年级 多级联动筛选
- **图片翻页阅读器** — 浏览器内翻页阅读，支持键盘快捷键（← →）和点击翻页
- **远程在线阅读** — 未下载的教材可直接从 CDN 代理加载图片在线阅读
- **R2 云存储**（可选） — 同步至 Cloudflare R2 对象存储
- **PM2 部署** — 附带 ecosystem 配置，一键生产部署

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 采集数据

```bash
# 采集元数据（直接请求 CDN API，无需浏览器）
python tch_material_crawler.py --collect

# 下载教材页面图片
python tch_material_crawler.py --download

# 一步完成：采集 + 下载
python tch_material_crawler.py --all

# 限制数量（测试用）
python tch_material_crawler.py --collect --max-books 100
python tch_material_crawler.py --download --max-books 10
```

### 3. 启动 Web 服务

```bash
# 开发模式
python app.py

# 生产模式（PM2）
pm2 start ecosystem.config.js
```

访问 http://localhost:5000

### 4. 查看统计

```bash
python -X utf8 tch_material_crawler.py --stats
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--collect` | 仅采集元数据 |
| `--download` | 仅下载页面图片 |
| `--all` | 采集 + 下载 |
| `--stats` | 查看数据库统计 |
| `--reset` | 重置失败状态为待下载 |
| `--fix-paths` | 修复因标题重复导致的路径冲突 |
| `--verify` | 验证已下载教材完整性（只读） |
| `--fix-incomplete` | 修复不完整的下载（删占位图+重置截断为待下载） |
| `--max-books N` | 限制处理数量 |
| `--start-from N` | 从第 N 本开始下载 |

## 项目结构

```
edu/
├── app.py                      # Flask Web 应用（首页 + 阅读器 + API）
├── tch_material_crawler.py     # 主采集器（元数据 + 页面图片下载）
├── r2_storage.py               # Cloudflare R2 存储模块（可选）
├── requirements.txt            # Python 依赖
├── ecosystem.config.js         # PM2 进程管理配置
├── tch_material.db             # SQLite 数据库（运行时生成）
├── textbook_pdfs/              # 页面图片（按书名分子目录）
├── textbook_covers/            # 封面图片（首页第1页副本）
├── img_cache/                  # 远程图片代理缓存（运行时生成）
└── templates/
    ├── index.html              # 首页（搜索 + 分类筛选 + 卡片网格）
    └── viewer.html             # 图片翻页阅读器
```

## 数据库设计

```sql
CREATE TABLE textbooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id      TEXT UNIQUE,          -- smartedu.cn 资源 UUID
    title           TEXT NOT NULL,        -- 教材名称
    subject         TEXT,                 -- 学科（语文、数学、英语...）
    phase           TEXT,                 -- 学段（小学、初中、高中...）
    grade           TEXT,                 -- 年级（一年级~高三）
    semester        TEXT,                 -- 册次（上册、下册、全一册）
    publisher       TEXT,                 -- 出版社
    cover_path      TEXT,                 -- 本地封面图片路径
    pdf_path        TEXT,                 -- 页面图片目录路径
    page_count      INTEGER,             -- 页数
    file_size       INTEGER,             -- 总文件大小（字节）
    download_status TEXT DEFAULT 'pending',  -- pending/downloaded/failed
    is_pinned       INTEGER DEFAULT 0,   -- 置顶标记
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
);
```

分类字段提取自 API 返回的 `tag_list`，通过 `tag_dimension_id` 映射：

| tag_dimension_id | 字段 | 含义 |
|-----------------|------|------|
| `zxxxd` | phase | 学段 |
| `zxxxk` | subject | 学科 |
| `zxxnj` | grade | 年级 |
| `zxxbb` | publisher | 版本 |
| `zxxcc` | semester | 册别 |

## API 接口

| 接口 | URL 模式 | 说明 |
|------|---------|------|
| 教材列表 | `https://s-file-{1,2,3}.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/part_{101~103}.json` | 分片 JSON，每片含数百本教材元数据 |
| 教材详情 | `https://s-file-{1,2,3}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{UUID}.json` | 含 `custom_properties.preview` 页面图片 URL |
| 页面图片 | `https://r{1,2,3}-ndr.ykt.cbern.com.cn/edu_product/esp/assets/{UUID}.t/zh-CN/{TS}/transcode/image/{PAGE}.jpg` | 高清页面图片 |

> **注意**：平台已关闭 PDF 直接下载（返回 403），本项目改为下载转码后的高清页面图片。

## 反爬策略

- 随机 User-Agent
- 请求间随机延迟 1-3 秒
- 每处理 50 本短暂休息 10-30 秒
- CDN 服务器轮询（1/2/3 号节点）

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.12 + Flask |
| 采集 | requests（元数据）+ Playwright（可选调试） |
| 数据库 | SQLite |
| 前端 | HTML + CSS + JavaScript |
| 云存储 | Cloudflare R2 + boto3（可选） |
| 进程管理 | PM2 |

## 下载状态说明

### 当前状态

| 状态 | 数量 | 说明 |
|------|------|------|
| 已下载 | 1906 | 页面图片完整下载 |
| 无法下载 | 731 | 源站未提供图片资源 |

### 731 本无法下载的原因

这些教材在国家智慧教育平台（smartedu.cn）上**本身就不提供在线翻页阅读**，属于源站限制，非代码问题：

| 类型 | 数量 | 具体原因 |
|------|------|----------|
| 体育与健康（含五•四学制） | 293 | API 返回 403 AccessDenied，所有 CDN 节点均不可访问 |
| 体育与健康教师用书 | 429 | 同上，API 返回 403，资源未公开 |
| 信息科技教学指南 | 9 | API 返回 200 但 `ti_items` 为空，源站未上传页面图片 |

### 如何验证

```bash
# 查看统计
python tch_material_crawler.py --stats

# 验证已下载教材的完整性
python tch_material_crawler.py --verify

# 修复不完整的下载（如截断的 100 页书）
python tch_material_crawler.py --fix-incomplete
python tch_material_crawler.py --download
```

### 给后续开发者的建议

如果你希望尝试下载这 731 本教材，可能的方向：

1. **等待源站更新** — 这些教材目前未公开图片资源，未来平台可能会补上
2. **尝试其他 API** — 当前使用的是 `s-file-{1,2,3}.ykt.cbern.com.cn`，可探索是否有其他接口返回图片数据
3. **PDF 下载** — 平台曾提供 PDF 直链但已关闭（返回 403），如果未来重新开放可恢复
4. **Selenium/Puppeteer 截图** — 对于平台上有在线阅读器但 API 不返回图片的书，理论上可以通过浏览器截图获取，但效率极低且容易被封

## 声明

所有资源版权归国家中小学智慧教育平台所有，本项目仅供个人学习研究使用。
