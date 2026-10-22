# End Clothing 70% Off 折扣爬虫

从 END. 中国区 Men / Women 促销页面抓取标注 **60%、65%、70% off** 的商品，并生成 JSON 与静态展示页面。

## 新版网站适配

END. 已迁移至 Shopify，原来的 `/cn/sale/all-sale`、`__NEXT_DATA__` 和商品 DOM 不再适用。当前入口：

- Men：https://www.endclothing.com/en-cn/collections/mens-sale
- Women：https://www.endclothing.com/en-cn/collections/womens-sale

抓取复用网站自身的两步请求：

1. 从集合页面读取当前 Algolia 公开搜索配置、中国区索引、集合 ID 和币种，通过 Algolia 查询有库存的集合商品，以商品维度去重并遍历分页。
2. 使用 `/en-cn/search?section_id=product-card-batch&q=...`，按已验证的每批 20 件读取商品卡片，提取名称、颜色、原价、售价、折扣标签、商品地址和图片地址。

折扣以卡片标签为准，不按价格比率推算。例如网站可能将 `$19 → $7` 标为 `65% off`。中国区当前采用 **USD**，输出带有 `currency` 字段，展示页根据该字段显示货币；旧数据缺失该字段时仍按 CNY 显示，不执行汇率换算。

## 安装与运行

需要 Python 3.9+ 和 Google Chrome。脚本沿用 Selenium 与 webdriver-manager 启动无头 Chrome；首次运行会下载匹配的 ChromeDriver。

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -u scrape_endclothing.py
```

依赖为 `requests`、`beautifulsoup4`、`lxml`、`selenium`、`webdriver-manager`。Men / Women 分别使用独立浏览器并行抓取，接口通过浏览器内的 `fetch` 请求，避免依赖商品卡片滚动加载。两类商品共享至少 1 秒的请求间隔；网络失败重试一次，普通 HTTP 429 至少退避 30 秒，并遵循 `Retry-After` 的秒数或日期。

本次实测直接 HTTP 请求会返回 Cloudflare 连接验证页，而浏览器内请求正常。浏览器遇到连接验证或持续限流时停止抓取、保留原文件，不自动处理 CAPTCHA；本地成功也不能保证 GitHub Actions 的不同网络出口可用。

运行完成后打开 `index.html`。顶部可切换 Men / Women，再按折扣、品牌和名称筛选；默认显示 Men 的 70% off 商品。

## 输出与完整性

- `endclothing_70off.json`：商品数据。
- `data.js`：供展示页读取的同一份数据及北京时间更新时间。
- 按商品类型与商品 URL 去重，移除旧商品 URL 的 `queryID` 追踪参数。
- 校验搜索页码、页容量、总页数、商品集合归属、跨页重复、卡片数量，以及目标商品名称、链接、图片、价格和币种。
- 启动失败或旧站数据迁移过程中任何分页失败，返回非零状态，不覆盖原文件。迁移必须完成整个抓取才会替换旧站商品。
- 后续抓取若部分分页失败，只在已有数据属于新版 USD 商品时保留该类型旧记录并合并成功页；日志会明确列出跳过页。这种结果可能包含未刷新或已下架的旧商品，不能视为全量更新。
- 日志输出集合商品数、卡片批次进度、匹配数量、跳过页和总耗时。

```json
[
  {
    "type": "men",
    "name": "New Balance Abzorb 1890 Sneaker - Dark Silver Metallic & Granite",
    "original_price": 209,
    "discounted_price": 63,
    "currency": "USD",
    "discount": "70% off",
    "url": "https://www.endclothing.com/en-cn/products/new-balance-abzorb-1890-sneaker-u18907ng",
    "image_url": "https://www.endclothing.com/cdn/shop/files/0d73797ce7405ba561c3a0b8327dd02500db7d36.jpg?v=1788611048&width=2100"
  }
]
```

GitHub Actions 每 3 小时运行，也可手动触发；先执行离线测试，再抓取并发布。同一分支串行执行，抓取返回非零状态时停止后续提交、部署。

抓取依赖网站公开页面配置、Algolia 搜索与 Shopify 卡片接口；网站再次改版、商品列表在抓取中变化、接口限流或分页被截断时会记录失败。按 `Ctrl+C` 中断不会保存中间结果；线程池会等待正在执行的请求退出。
