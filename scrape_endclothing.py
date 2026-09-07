import requests
from bs4 import BeautifulSoup
import re
import json
import time
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit, urlunsplit
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE_URLS = {
    "men": "https://www.endclothing.com/cn/sale/all-sale",
    "women": "https://www.endclothing.com/cn/women/sale/all-sale",
}
OUTPUT_FILE = "endclothing_70off.json"
DATA_JS_FILE = "data.js"
PRODUCT_CARD_SELECTOR = '#plpBody a[data-test-id="ProductCard__ProductCardSC"]'
PAGE_ATTEMPTS = 2
PAGE_INTERVAL = 1
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def normalize_product_url(url):
    """Remove the per-search queryID without changing other query parameters."""
    parts = urlsplit(url)
    query = '&'.join(part for part in parts.query.split('&')
                     if unquote(part.partition('=')[0]) != 'queryID')
    return urlunsplit(parts._replace(query=query))


def load_existing_data():
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Existing records without a type belong to Men.
                for item in data:
                    item.setdefault('type', 'men')
                    item['url'] = normalize_product_url(item['url'])
                return {(item['type'], item['url']): item for item in data}
        except Exception as e:
            raise RuntimeError(f"Cannot read existing data: {e}") from e
    return {}

def save_data(products_dict):
    # Save as JSON
    products_list = list(products_dict.values())
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(products_list, f, ensure_ascii=False, indent=2)
    
    # Save as JS for HTML view with update time
    json_str = json.dumps(products_list, ensure_ascii=False)
    # 获取北京时间 (UTC+8)
    beijing_tz = timezone(timedelta(hours=8))
    update_time = datetime.now(beijing_tz).strftime('%Y-%m-%d %H:%M')   
    with open(DATA_JS_FILE, 'w', encoding='utf-8') as f:
        f.write(f"window.products = {json_str};\n")
        f.write(f'window.updateTime = "{update_time}";')

def create_chrome_options():
    """创建Chrome选项，抑制不必要的警告和日志"""
    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument('--headless=new')  # 使用新版headless模式
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-software-rasterizer')
    options.add_argument('--disable-webgl')
    options.add_argument('--disable-webgl2')
    options.add_argument('--log-level=3')  # 只显示严重错误
    options.add_argument('--silent')
    options.add_argument(f'user-agent={HEADERS["User-Agent"]}')
    
    # 抑制 DevTools 和其他日志
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    options.add_experimental_option('useAutomationExtension', False)
    
    return options

def get_page_info(soup, product_type, page_num, cards=None):
    """Read authoritative pagination and reject redirects or incomplete lists."""
    try:
        data = json.loads(soup.find('script', id='__NEXT_DATA__').string)
        query = data['query']
        expected_route = BASE_URLS[product_type].split('/cn/', 1)[1].split('/')
        if query.get('countryCode') != 'cn' or query.get('route') != expected_route:
            raise ValueError('Unexpected country or product type')
        info = data['props']['initialProps']['pageProps']['initialAlgoliaState']['results']
        for key in ('nbHits', 'nbPages', 'page', 'hitsPerPage'):
            if type(info[key]) is not int or info[key] < 0:
                raise ValueError(f'Invalid pagination field: {key}')
        if info['page'] != page_num - 1 or info['hitsPerPage'] == 0:
            raise ValueError('Unexpected page number or page size')
        total_pages = math.ceil(info['nbHits'] / info['hitsPerPage'])
        if info['nbPages'] != total_pages:
            raise ValueError('Inconsistent total pages')
        if (total_pages and page_num > total_pages) or (not total_pages and page_num != 1):
            raise ValueError('Page outside the result set')
        expected_count = min(info['hitsPerPage'], max(0, info['nbHits'] - info['page'] * info['hitsPerPage']))
        if cards is None:
            cards = soup.select(PRODUCT_CARD_SELECTOR)
        if len(info['hits']) != expected_count or len(cards) != expected_count:
            raise ValueError('Incomplete product list')
        if any(not isinstance(hit.get('sale_percentage'), str) for hit in info['hits']):
            raise ValueError('Missing product discount metadata')
        expected_targets = sum(hit['sale_percentage'] in ('60%', '65%', '70%') for hit in info['hits'])
        actual_targets = sum(bool(re.search(r'(?:70|65|60)% off', card.get_text()))
                             for card in cards)
        if actual_targets != expected_targets:
            raise ValueError('Incomplete discount labels')
        return info
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f'[{product_type}] Invalid page {page_num}: {e}') from e


def page_is_ready(driver, product_type, page_num):
    # Check URLs rather than image downloads; lazy images need not finish loading.
    return driver.execute_script(r"""
        const root = document.querySelector('#plpBody');
        const script = document.getElementById('__NEXT_DATA__');
        if (!root || !script) return false;
        try {
            const data = JSON.parse(script.textContent);
            const info = data.props.initialProps.pageProps.initialAlgoliaState.results;
            if (data.query.countryCode !== 'cn' ||
                data.query.route.join('/') !== arguments[0] ||
                info.page !== arguments[1]) return false;
            const cards = Array.from(document.querySelectorAll(arguments[2]));
            if (cards.length !== info.hits.length) return false;
            if (!cards.length) return info.nbHits === 0;
            if (info.hits.some(hit => typeof hit.sale_percentage !== 'string')) return false;
            const targetCount = info.hits.filter(hit => ['60%', '65%', '70%'].includes(hit.sale_percentage)).length;
            const targetCards = cards.filter(card => /(?:70|65|60)% off/.test(card.textContent));
            if (targetCards.length !== targetCount) return false;
            return cards.every(card => {
                const name = card.querySelector('[data-test-id="ProductCard__PlpName"]');
                if (!name || !name.textContent.trim() || !card.textContent.includes('CN¥')) return false;
                if (!/(?:70|65|60)% off/.test(card.textContent)) return true;
                const fullPrice = card.querySelector('[data-test-id="ProductCard__ProductFullPrice"]');
                const finalPrice = card.querySelector('[data-test-id="ProductCard__ProductFinalPrice"]');
                if (!fullPrice || !finalPrice || !fullPrice.textContent.includes('CN¥') ||
                    !finalPrice.textContent.includes('CN¥')) return false;
                const img = card.querySelector('img');
                if (!img) return false;
                const sources = [img.getAttribute('src'), img.getAttribute('data-src'),
                    (img.getAttribute('srcset') || '').trim().split(/\s+/)[0]];
                return sources.some(src => /^https?:\/\//.test(src || ''));
            });
        } catch (_) {
            return false;
        }
    """, BASE_URLS[product_type].split('/cn/', 1)[1], page_num - 1, PRODUCT_CARD_SELECTOR)


def get_page_data(page_num, driver, product_type='men'):
    """Load and validate once, returning reusable cards and pagination metadata."""
    url = f"{BASE_URLS[product_type]}?page={page_num}"
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            driver.get(url)
            loaded = time.perf_counter()
            WebDriverWait(driver, 15, poll_frequency=0.25).until(
                lambda current: page_is_ready(current, product_type, page_num)
            )
            ready = time.perf_counter()
            soup = BeautifulSoup(driver.page_source, 'lxml')
            cards = soup.select(PRODUCT_CARD_SELECTOR)
            info = get_page_info(soup, product_type, page_num, cards)
            print(f"[{product_type}] page {page_num}: load={loaded - started:.2f}s "
                  f"wait={ready - loaded:.2f}s parse={time.perf_counter() - ready:.2f}s", flush=True)
            return cards, info
        except Exception as e:
            print(f"[{product_type}] page {page_num} attempt {attempt}/{PAGE_ATTEMPTS} failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            if attempt == PAGE_ATTEMPTS:
                raise RuntimeError(f'[{product_type}] Page {page_num} failed after retries') from e
            time.sleep(PAGE_INTERVAL)


def extract_products(cards, product_type='men'):
    """提取所有 70% 和 60% 65% off 的产品信息"""
    products = []
    discount_spans = [text for card in cards
                      for text in card.find_all(string=re.compile(r'70% off|60% off|65% off'))]
    
    if not discount_spans:
        return []

    for text_node in discount_spans:
        parent = text_node.parent
        while parent and parent.name != 'a':
            parent = parent.parent
        
        if parent and parent.name == 'a':
            link = parent
            href = link.get('href')
            
            full_url = f"https://www.endclothing.com{href}" if href.startswith('/') else href
            full_url = normalize_product_url(full_url)
            content = link.get_text(strip=True)
            
            # Choose a real URL; placeholders may coexist with lazy-load URLs.
            img_tag = link.find('img')
            img_url = ""
            if img_tag:
                srcset_parts = (img_tag.get('srcset') or '').strip().split()
                sources = [img_tag.get('src'), img_tag.get('data-src'),
                           srcset_parts[0].rstrip(',') if srcset_parts else None]
                img_url = next((src for src in sources if src and src.startswith(('http://', 'https://'))), '')
            
            match = re.search(r'(.*)(CN¥[\d,]+)(CN¥[\d,]+)((?:70%|65%|60%) off)$', content)
            if match:
                name = match.group(1).strip()
                original_price_str = match.group(2)
                sale_price_str = match.group(3)
                discount_str = match.group(4)
                
                original_price = int(re.sub(r'[^\d]', '', original_price_str))
                discounted_price = int(re.sub(r'[^\d]', '', sale_price_str))
                
                products.append({
                    "type": product_type,
                    "name": name,
                    "original_price": original_price,
                    "discounted_price": discounted_price,
                    "discount": discount_str,
                    "url": full_url,
                    "image_url": img_url
                })
            else:
                # Fallback for cases where the regex doesn't match
                name_match = re.search(r'(.*)(CN¥)', content)
                name = name_match.group(1).strip() if name_match else content
                # Try to find which discount it is from the text node
                discount_val = "70% off"
                if "65% off" in text_node: discount_val = "65% off"
                elif "60% off" in text_node: discount_val = "60% off"
                
                products.append({
                    "type": product_type,
                    "name": name,
                    "raw_content": content,
                    "url": full_url,
                    "discount": discount_val,
                    "image_url": img_url
                })
    return products

def scrape_type(product_type, driver_path):
    """Each worker owns its browser and keeps all results in memory."""
    started = time.perf_counter()
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(driver_path), options=create_chrome_options())
        driver.set_page_load_timeout(30)
        cards, info = get_page_data(1, driver, product_type)
        total_pages = info['nbPages']
        print(f"[{product_type}] products={info['nbHits']} page_size={info['hitsPerPage']} "
              f"pages={total_pages}", flush=True)
        products_dict = {}
        for page in range(1, total_pages + 1):
            if page > 1:
                time.sleep(PAGE_INTERVAL)
                cards, info = get_page_data(page, driver, product_type)
            products = extract_products(cards, product_type)
            expected_targets = sum(hit['sale_percentage'] in ('60%', '65%', '70%') for hit in info['hits'])
            if len(products) != expected_targets:
                raise ValueError(f'[{product_type}] Incomplete discount extraction on page {page}')
            if any('original_price' not in p or 'discounted_price' not in p or not p.get('image_url')
                   for p in products):
                raise ValueError(f'[{product_type}] Incomplete product data on page {page}')
            for product in products:
                products_dict[(product_type, product['url'])] = product
            print(f"[{product_type}] page {page}/{total_pages}: matched={len(products)} "
                  f"collected={len(products_dict)}", flush=True)
        print(f"[{product_type}] complete: products={len(products_dict)} "
              f"elapsed={time.perf_counter() - started:.2f}s", flush=True)
        return products_dict
    finally:
        if driver is not None:
            driver.quit()


def main():
    started = time.perf_counter()
    try:
        existing = load_existing_data()
        initial_count = len(existing)
        initial_counts = {
            product_type: sum(1 for product in existing.values()
                              if product.get('type', 'men') == product_type)
            for product_type in BASE_URLS
        }
        print(f"Loaded {initial_count} existing products.", flush=True)
        # Install once before starting workers to avoid concurrent cache writes.
        driver_path = ChromeDriverManager().install()
        results = {}
        failed_types = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(scrape_type, product_type, driver_path): product_type
                       for product_type in BASE_URLS}
            for future in as_completed(futures):
                product_type = futures[future]
                try:
                    results[product_type] = future.result()
                except Exception as e:
                    failed_types.append(product_type)
                    print(f"[{product_type}] FAILED: {e}", flush=True)
        if failed_types:
            print(f"Incomplete scrape ({', '.join(failed_types)}). Existing files retained; "
                  "publication blocked.", flush=True)
            return 1

        # Keep output order deterministic regardless of which worker finishes first.
        all_products_dict = {}
        for product_type in BASE_URLS:
            all_products_dict.update(results[product_type])
        save_data(all_products_dict)
        final_count = len(all_products_dict)
        diff = final_count - initial_count
        change_desc = f"增加 {diff}" if diff > 0 else f"减少 {abs(diff)}" if diff < 0 else "无变化"
        final_counts = {
            product_type: len(results[product_type])
            for product_type in BASE_URLS
        }
        change_descs = {}
        for product_type in BASE_URLS:
            type_diff = final_counts[product_type] - initial_counts[product_type]
            change_descs[product_type] = (
                f"增加 {type_diff}" if type_diff > 0
                else f"减少 {abs(type_diff)}" if type_diff < 0
                else "无变化"
            )
        env_file = os.getenv('GITHUB_ENV')
        if env_file:
            with open(env_file, 'a', encoding='utf-8') as f:
                f.write(f"PRODUCT_COUNT={final_count}\n")
                f.write(f"PRODUCT_CHANGE_DESC={change_desc}\n")
                f.write(f"MEN_PRODUCT_COUNT={final_counts['men']}\n")
                f.write(f"MEN_CHANGE_DESC={change_descs['men']}\n")
                f.write(f"WOMEN_PRODUCT_COUNT={final_counts['women']}\n")
                f.write(f"WOMEN_CHANGE_DESC={change_descs['women']}\n")
        print(f"Complete: products={final_count} elapsed={time.perf_counter() - started:.2f}s", flush=True)
        return 0
    except KeyboardInterrupt:
        print('Interrupted. No partial scrape will be published.', flush=True)
        return 1
    except Exception as e:
        print(f'Scrape failed: {type(e).__name__}: {e}', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
