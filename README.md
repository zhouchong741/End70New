# End Clothing 70% Off 折扣爬虫

这是一个 Python 脚本，用于自动从 End Clothing (CN) 网站的促销页面抓取所有折扣力度为 **70% off** 的商品，并将结果保存为 JSON 文件。

## 功能特点

- **动态页面抓取** - 使用 Selenium 模拟浏览器行为，完美支持动态加载的内容（如图片）
- **自动遍历** - 自动识别总页数并遍历所有促销页面
- **精准筛选** - 筛选出折扣为 "70% off" 的商品
- **完整信息** - 提取商品名称、原价、折后价、购买链接和**高清产品图片**
- **断点续传** - 支持增量抓取，自动去重
- **可视化展示** - 提供 HTML 页面直接查看抓取结果

## 环境要求

- Python 3.x
- Google Chrome 浏览器
- 需要安装以下 Python 库：
  - `selenium`
  - `webdriver-manager`
  - `beautifulsoup4`
  - `requests`

## 安装步骤

1. 确保已安装 Python 和 Google Chrome 浏览器。
2. 安装所需的依赖库：

```bash
pip install selenium webdriver-manager beautifulsoup4 requests
```

## 使用方法

1. 打开终端 (Terminal) 或命令行窗口。
2. 切换到脚本所在目录。
3. 运行脚本：

```bash
python scrape_endclothing.py
```

脚本会自动下载匹配的 ChromeDriver 并启动无头浏览器进行抓取。

4. **查看结果**：
   抓取完成后，直接在浏览器中打开 `view_products.html` 文件，即可通过精美的网格视图浏览所有打折商品。

## 输出结果

结果将保存在当前目录下的 `endclothing_70off.json` (数据源) 和 `data.js` (用于前端展示) 文件中。

**JSON 数据格式示例：**

```json
[
  {
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
由于 End Clothing 网站的产品图片采用懒加载和 JavaScript 动态渲染，传统的 `requests` 库无法获取图片信息。本脚本使用 `Selenium` 配合 `ChromeDriver` 加载完整的网页 DOM，确保能准确提取到高分辨率的产品图片 URL。

### 智能分页
脚本会自动检测搜索结果的总数量，计算总页数，并逐页抓取。

## 注意事项

- 脚本运行期间会启动 Chrome 进程，请保持网络连接畅通。
- 首次运行会自动下载 ChromeDriver，可能需要一点时间。
- 按 `Ctrl+C` 可以中断脚本，已抓取的数据会自动保存。
