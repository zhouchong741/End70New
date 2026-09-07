# End Clothing 70% Off 折扣爬虫

这是一个 Python 脚本，用于自动从 End Clothing (CN) 网站的 Men 和 Women 促销页面抓取 **60%、65%、70% off** 的商品，并将结果保存为 JSON 文件。

## 功能特点

- **动态页面抓取** - 使用 Selenium 模拟浏览器行为，完美支持动态加载的内容（如图片）
- **自动遍历** - 自动识别总页数并遍历所有促销页面
- **双类型并行抓取** - Men 和 Women 各使用独立浏览器，按类型和去除 `queryID` 后的商品链接去重，完成后统一保存
- **精准筛选** - 筛选出折扣为 "60% off"、"65% off"、"70% off" 的商品
- **完整信息** - 提取商品名称、原价、折后价、购买链接和**高清产品图片**
- **完整性保护** - 每轮完整抓取；任一类型失败时保留原数据，并阻止后续提交和部署
- **可视化展示** - 提供 HTML 页面直接查看抓取结果

## 环境要求

- Python 3.x
- Google Chrome 浏览器
- 需要安装以下 Python 库：
  - `selenium`
  - `webdriver-manager`
  - `beautifulsoup4`
  - `lxml`
  - `requests`

## 安装步骤

1. 确保已安装 Python 和 Google Chrome 浏览器。
2. 安装所需的依赖库：

```bash
pip install -r requirements.txt
```

## 使用方法

1. 打开终端 (Terminal) 或命令行窗口。
2. 切换到脚本所在目录。
3. 运行脚本：

```bash
python scrape_endclothing.py
```

脚本会自动下载匹配的 ChromeDriver，并启动两个独立的无头浏览器并行抓取。

4. **查看结果**：
   抓取完成后，直接在浏览器中打开 `index.html` 文件，即可浏览所有打折商品。顶部可切换 Men / Women，再按折扣、品牌和名称筛选；默认显示 Men 的 70% off 商品，旧数据未包含类型时按 Men 展示。

抓取来源：

- Men：https://www.endclothing.com/cn/sale/all-sale
- Women：https://www.endclothing.com/cn/women/sale/all-sale

GitHub Actions 每 3 小时自动运行，也可手动触发。同一分支的任务串行执行，避免重叠发布；每次运行先执行离线回归测试，再抓取并发布。

## 输出结果

结果将保存在当前目录下的 `endclothing_70off.json` (数据源) 和 `data.js` (用于前端展示) 文件中。

**JSON 数据格式示例：**

```json
[
  {
    "type": "men",
    "name": "Adidas TaekwondoWhite & Black",
    "original_price": 719,
    "discounted_price": 216,
    "discount": "70% off",
    "url": "https://www.endclothing.com/cn/adidas-taekwondo-jq4775.html...",
    "image_url": "https://media.endclothing.com/media/f_auto,q_auto:eco,w_1600/..."
  },
  ...
]
```

## 技术实现

### Selenium 动态渲染
商品图片节点由页面脚本生成。本脚本使用 Selenium 的 `eager` 加载策略，等待商品列表、名称、价格以及目标折扣商品的图片 URL 就绪，不等待图片下载完成，也不再每页固定等待 2 秒。每个浏览器的分页请求仍保留 1 秒间隔。

### 智能分页
脚本读取页面 `__NEXT_DATA__` 中的 `nbHits`、`nbPages`、`hitsPerPage` 和页码，并核对当前类型及商品卡片数量，避免把列表外链接计入每页容量。分页信息缺失或不一致时按失败处理，不再默认抓取 100 页。

HTML 使用 `lxml` 解析；每页只选择一次商品卡片、校验一次分页信息，提取商品时复用已有结果。保存商品链接和读取旧数据时移除动态 `queryID`，保留其他查询参数和锚点，避免搜索追踪参数变化导致重复商品或无意义的数据差异。旧文件会在下次完整抓取成功后更新，数据格式不变。

### 失败处理与耗时

单页加载超时为 30 秒，商品就绪等待最多 15 秒，单页失败重试 1 次。两个类型全部成功后才统一写入数据并返回成功；失败返回非零退出码，GitHub Actions 不会继续提交和部署中间结果。日志按类型输出每页加载、等待、解析耗时及总耗时。

离线回归测试（不启动 Chrome、不请求网站）：

```bash
python -m unittest discover -s tests -v
```

## 注意事项

- 脚本运行期间会启动 Chrome 进程，请保持网络连接畅通。
- 首次运行会自动下载 ChromeDriver，可能需要一点时间。
- 按 `Ctrl+C` 中断时不会发布部分数据；线程池会等待正在运行的抓取任务退出后清理浏览器。
