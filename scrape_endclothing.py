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
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE_URLS = {
    "men": "https://www.endclothing.com/en-cn/collections/mens-sale",
    "women": "https://www.endclothing.com/en-cn/collections/womens-sale",
}
OUTPUT_FILE = "endclothing_70off.json"
DATA_JS_FILE = "data.js"
PRODUCT_CARD_SELECTOR = '[data-product-card-handle]'
TARGET_DISCOUNTS = {"60% off", "65% off", "70% off"}
HITS_PER_PAGE = 1000
CARD_BATCH_SIZE = 20
PAGE_ATTEMPTS = 2
PAGE_INTERVAL = 1
REQUEST_INTERVAL = 1
REQUEST_LOCK = Lock()
next_request_at = 0
HEADERS = {
    'Accept': 'text/html,application/json'
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

class BrowserSession:
    """Make storefront requests inside a normal Chrome JavaScript session."""

    def __init__(self, driver_path):
        options = Options()
        options.page_load_strategy = 'eager'
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        self.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        self.driver.set_page_load_timeout(30)
        self.driver.set_script_timeout(35)
        self.headers = dict(HEADERS)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.driver.quit()

    def request(self, method, url, timeout=30, params=None, headers=None, **kwargs):
        if '/collections/' in urlsplit(url).path:
            self.driver.get(url)
            WebDriverWait(self.driver, timeout).until(
                lambda driver: driver.execute_script('return !!window.algoliaShopify?.config?.app_id')
            )
            # The first visit can redirect by IP; re-open the explicit country
            # URL after initialization, then let get_storefront_config validate it.
            if self.driver.execute_script('return window.Shopify?.country') != 'CN':
                self.driver.get(url)
                WebDriverWait(self.driver, timeout).until(
                    lambda driver: driver.execute_script('return !!window.algoliaShopify?.config?.app_id')
                )
            result = {'status': 200, 'url': self.driver.current_url, 'body': self.driver.page_source, 'headers': {}}
        else:
            if params:
                url += '?' + urlencode(params)
            options = {'method': method, 'headers': dict(self.headers, **(headers or {}))}
            if 'json' in kwargs:
                options['headers']['Content-Type'] = 'application/json'
                options['body'] = json.dumps(kwargs['json'])
            result = self.driver.execute_async_script('''
                const [url, options, timeout, done] = arguments;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                fetch(url, {...options, signal: controller.signal})
                    .then(async response => done({status: response.status, url: response.url,
                        body: await response.text(), headers: Object.fromEntries(response.headers)}))
                    .catch(error => done({error: error.name}))
                    .finally(() => clearTimeout(timer));
            ''', url, options, timeout * 1000)
            if 'error' in result:
                raise requests.RequestException(f'Browser request failed: {result["error"]}')
        response = requests.Response()
        response.status_code = result['status']
        response.url = result['url']
        response._content = result['body'].encode('utf-8')
        response.encoding = 'utf-8'
        response.headers.update(result['headers'])
        return response


class StorefrontVerificationRequired(RuntimeError):
    pass


def request(session, method, url, **kwargs):
    """Retry transient network failures without logging the public search key."""
    global next_request_at
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        with REQUEST_LOCK:
            time.sleep(max(0, next_request_at - time.monotonic()))
            next_request_at = time.monotonic() + REQUEST_INTERVAL
        try:
            response = session.request(method, url, timeout=30, **kwargs)
            if response.status_code in (403, 429) and 'verifying your connection' in response.text.lower():
                raise StorefrontVerificationRequired('Storefront requires browser connection verification')
            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After', '30')
                try:
                    delay = float(retry_after) if retry_after.isdigit() else (
                        parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 30
                delay = max(30, delay)
                with REQUEST_LOCK:
                    next_request_at = max(next_request_at, time.monotonic() + delay)
                if attempt < PAGE_ATTEMPTS:
                    print(f'Rate limited; all workers waiting at least {delay:.0f}s.', flush=True)
                    continue
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == PAGE_ATTEMPTS:
                raise
            time.sleep(PAGE_INTERVAL)


def read_json_assignment(html, pattern):
    match = re.search(pattern, html)
    if not match:
        raise ValueError('Missing storefront configuration')
    return json.JSONDecoder().raw_decode(html[match.end():].lstrip())[0]


def get_storefront_config(session, product_type):
    response = request(session, 'GET', BASE_URLS[product_type])
    if urlsplit(response.url).path != urlsplit(BASE_URLS[product_type]).path:
        raise ValueError(f'[{product_type}] Unexpected storefront redirect: {urlsplit(response.url).path}')
    html = response.text
    config = read_json_assignment(html, r'window\.algoliaShopify\.config\s*=\s*(?=\{)')
    country = read_json_assignment(html, r'Shopify\.country\s*=')
    currency = read_json_assignment(html, r'Shopify\.currency\s*=')['active']
    locale = read_json_assignment(html, r'Shopify\.locale\s*=')
    market = re.search(r'algoliaShopify\.shopify\.market\s*=.*?"id"\s*:\s*(\d+)', html, re.S)
    collection = re.search(r'algoliaShopify\.shopify\.collection_id\s*=\s*(\d+)', html)
    if country != 'CN' or locale != 'en' or currency not in ('USD', 'CNY') or not market or not collection:
        raise ValueError('Unexpected country, currency or collection configuration')
    mapping = next((item for item in config['markets_mapping']
                    if str(item['id']) == market.group(1) and item['locale'] == locale
                    and country in item['all_regions']), None)
    if not mapping or mapping['base_currency'] != currency or not config['markets_indexing_enabled']:
        raise ValueError('Cannot determine China market index')
    suffix = '_' + mapping['suffix'] if mapping['suffix'] else ''
    return {
        'app_id': config['app_id'],
        'search_api_key': config['search_api_key'],
        'index': config['index_prefix'] + 'products' + suffix,
        'collection_id': collection.group(1),
        'collection': urlsplit(response.url).path.rsplit('/', 1)[1],
        'currency': currency,
    }


def get_page_info(info, page_num, config):
    """Reject truncated pagination and duplicate variants before processing cards."""
    for key in ('nbHits', 'nbPages', 'page', 'hitsPerPage'):
        if type(info.get(key)) is not int or info[key] < 0:
            raise ValueError(f'Invalid pagination field: {key}')
    if info['page'] != page_num - 1 or info['hitsPerPage'] != HITS_PER_PAGE:
        raise ValueError('Unexpected page number or page size')
    total_pages = math.ceil(info['nbHits'] / info['hitsPerPage'])
    if info['nbPages'] != total_pages:
        raise ValueError('Search pagination is capped or inconsistent')
    if (total_pages and page_num > total_pages) or (not total_pages and page_num != 1):
        raise ValueError('Page outside the result set')
    expected_count = min(info['hitsPerPage'], max(0, info['nbHits'] - info['page'] * info['hitsPerPage']))
    hits = info['hits']
    if len(hits) != expected_count:
        raise ValueError('Incomplete search result list')
    handles = []
    for hit in hits:
        handle = hit.get('handle')
        if not isinstance(handle, str) or not re.fullmatch(r'[a-z0-9-]+', handle):
            raise ValueError('Missing or invalid product handle')
        if int(config['collection_id']) not in hit.get('collection_ids', []) or not hit.get('inventory_available'):
            raise ValueError('Unexpected collection or unavailable product')
        handles.append(handle)
    if len(set(handles)) != len(handles):
        raise ValueError('Duplicate products in search results')
    return info


def get_page_data(page_num, session, config):
    params = urlencode({
        'hitsPerPage': HITS_PER_PAGE,
        'page': page_num - 1,
        'distinct': 'true',
        'filters': f'collection_ids:"{config["collection_id"]}" AND inventory_available:true',
        'ruleContexts': json.dumps([config['collection'], 'shopify_default_collection']),
    })
    response = request(session, 'POST',
                       f'https://{config["app_id"].lower()}-3.algolianet.com/1/indexes/*/queries',
                       headers={'X-Algolia-Application-Id': config['app_id'],
                                'X-Algolia-API-Key': config['search_api_key']},
                       json={'requests': [{'indexName': config['index'], 'params': params}]})
    return get_page_info(response.json()['results'][0], page_num, config)


def get_product_cards(session, handles):
    response = request(session, 'GET', 'https://www.endclothing.com/en-cn/search',
                       params={'q': ' '.join(handles), 'section_id': 'product-card-batch'})
    if urlsplit(response.url).path != '/en-cn/search':
        raise ValueError('Unexpected product card region redirect')
    soup = BeautifulSoup(response.text, 'lxml')
    cards = soup.select(PRODUCT_CARD_SELECTOR)
    actual = [card.get('data-product-card-handle') for card in cards]
    if len(actual) != len(handles) or set(actual) != set(handles):
        raise ValueError('Incomplete product card batch')
    if any(not card.select_one('article.prd-Card') for card in cards):
        raise ValueError('Product card markup missing')
    return cards


def extract_price(card, selector, currency):
    node = card.select_one(selector)
    symbol = {'USD': '$', 'CNY': 'CN¥'}[currency]
    match = re.search(re.escape(symbol) + r'\s*([0-9,]+(?:\.[0-9]{1,2})?)(?![0-9.])',
                      node.get_text(' ', strip=True) if node else '')
    if not match:
        raise ValueError(f'Missing {currency} price: {selector}')
    value = float(match.group(1).replace(',', ''))
    return int(value) if value.is_integer() else value


def extract_products(cards, product_type='men', currency='USD'):
    """Use the storefront's discount labels, not rounded price-ratio guesses."""
    products = []
    for card in cards:
        label = card.select_one('.prd-Price_Discount')
        discount = label.get_text(' ', strip=True) if label else ''
        if not discount and card.select_one('.prd-Price_SalePrice'):
            raise ValueError('Missing sale discount label')
        if discount not in TARGET_DISCOUNTS:
            continue
        title = card.select_one('.prd-Card_Title')
        byline = card.select_one('.prd-Card_Byline')
        link = card.select_one('a.prd-Card_Image')
        image = link.select_one('img') if link else None
        name = title.get_text(' ', strip=True) if title else ''
        if not name:
            raise ValueError('Missing target product title')
        if byline and byline.get_text(strip=True):
            name += ' - ' + byline.get_text(' ', strip=True)
        path = '/en-cn/products/' + card['data-product-card-handle']
        if not name or not link or link.get('href') != path or not image or not image.get('src'):
            raise ValueError('Incomplete target product name, link or image')
        image_url = urljoin('https://www.endclothing.com', image['src'])
        if urlsplit(image_url).scheme != 'https' or not urlsplit(image_url).netloc:
            raise ValueError('Invalid product image URL')
        original_price = extract_price(card, '.prd-Price_RegularPrice', currency)
        discounted_price = extract_price(card, '.prd-Price_SalePrice', currency)
        if not 0 < discounted_price < original_price:
            raise ValueError('Invalid sale prices')
        products.append({
            'type': product_type,
            'name': name,
            'original_price': original_price,
            'discounted_price': discounted_price,
            'currency': currency,
            'discount': discount,
            'url': 'https://www.endclothing.com' + path,
            'image_url': image_url,
        })
    return products


def scrape_type(product_type, driver_path):
    """Retrieve the same search results and card sections used by the new site."""
    started = time.perf_counter()
    products_dict = {}
    skipped_pages = []
    with BrowserSession(driver_path) as session:
        session.headers.update(HEADERS)
        config = get_storefront_config(session, product_type)
        first_info = get_page_data(1, session, config)
        total_pages = first_info['nbPages']
        seen_handles = set()
        print(f'[{product_type}] products={first_info["nbHits"]} pages={total_pages} '
              f'currency={config["currency"]}', flush=True)
        for page in range(1, total_pages + 1):
            try:
                if page > 1:
                    time.sleep(PAGE_INTERVAL)
                info = first_info if page == 1 else get_page_data(page, session, config)
                if (info['nbHits'], info['nbPages']) != (first_info['nbHits'], total_pages):
                    raise ValueError('Catalog changed during pagination; retry a fresh run')
                handles = [hit['handle'] for hit in info['hits']]
                if seen_handles.intersection(handles):
                    raise ValueError('Repeated products across pages')
                page_products = []
                for offset in range(0, len(handles), CARD_BATCH_SIZE):
                    batch = handles[offset:offset + CARD_BATCH_SIZE]
                    cards = get_product_cards(session, batch)
                    page_products.extend(extract_products(cards, product_type, config['currency']))
                    if offset % (CARD_BATCH_SIZE * 10) == 0:
                        print(f'[{product_type}] page {page}/{total_pages}: '
                              f'cards={min(offset + CARD_BATCH_SIZE, len(handles))}/{len(handles)}', flush=True)
                seen_handles.update(handles)
                products_dict.update({(product_type, product['url']): product for product in page_products})
                print(f'[{product_type}] page {page}/{total_pages}: matched={len(page_products)} '
                      f'collected={len(products_dict)}', flush=True)
            except Exception as e:
                if isinstance(e, StorefrontVerificationRequired):
                    raise
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429:
                    raise RuntimeError(f'[{product_type}] Rate limit persists; stopping this type') from e
                skipped_pages.append(page)
                print(f'[{product_type}] SKIP page {page}: {type(e).__name__}: {e}', flush=True)
        print(f'[{product_type}] products={len(products_dict)} skipped_pages={skipped_pages} '
              f'elapsed={time.perf_counter() - started:.2f}s', flush=True)
    return products_dict, skipped_pages


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
        driver_path = ChromeDriverManager().install()
        results = {}
        failed_types = []
        skipped_by_type = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(scrape_type, product_type, driver_path): product_type
                       for product_type in BASE_URLS}
            for future in as_completed(futures):
                product_type = futures[future]
                try:
                    products, skipped_pages = future.result()
                    if skipped_pages:
                        skipped_by_type[product_type] = skipped_pages
                        # Without page ownership, retain this type's old products to avoid false removals.
                        retained = {key: item for key, item in existing.items() if key[0] == product_type}
                        if not retained or any(item.get('currency') != 'USD' or
                                               not item['url'].startswith('https://www.endclothing.com/en-cn/products/')
                                               for item in retained.values()):
                            raise ValueError('Cannot merge an incomplete new-site scrape with legacy data')
                        retained.update(products)
                        products = retained
                        print(f"[{product_type}] WARNING: skipped pages {skipped_pages}; "
                              "merging valid pages with existing products.", flush=True)
                    results[product_type] = products
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
        status = 'Published with skipped pages' if skipped_by_type else 'Complete'
        print(f"{status}: products={final_count} skipped_pages={skipped_by_type} "
              f"elapsed={time.perf_counter() - started:.2f}s", flush=True)
        return 0
    except KeyboardInterrupt:
        print('Interrupted. No partial scrape will be published.', flush=True)
        return 1
    except Exception as e:
        print(f'Scrape failed: {type(e).__name__}: {e}', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
