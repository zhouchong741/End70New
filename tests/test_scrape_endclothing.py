import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

import scrape_endclothing as scraper


# Structure and prices from the storefront on 2026-10-22; image path is synthetic.
CARD = '''<div data-product-card-handle="beams-japan-toothbrush-5674-0229-546-01">
<article class="prd-Card">
<a class="prd-Card_Image" href="/en-cn/products/beams-japan-toothbrush-5674-0229-546-01">
<img src="//www.endclothing.com/cdn/shop/files/toothbrush.jpg?width=2100"></a>
<h3 class="prd-Card_Title">BEAMS JAPAN Toothbrush</h3>
<div class="prd-Card_Byline">White</div>
<s class="prd-Price_RegularPrice"><span>Regular price</span>$19</s>
<span class="prd-Price_SalePrice"><span>Sale price</span>$7</span>
<span class="prd-Price_Discount">65% off</span>
</article></div>'''


class ScraperTests(unittest.TestCase):
    def cards(self, html=CARD):
        return BeautifulSoup(html, 'lxml').select(scraper.PRODUCT_CARD_SELECTOR)

    def response(self, text, url=None):
        return Mock(text=text, status_code=200, url=url or 'https://www.endclothing.com/en-cn/search')

    def info(self):
        return {'nbHits': 1, 'nbPages': 1, 'page': 0, 'hitsPerPage': 1000,
                'hits': [{'handle': 'sample-product', 'collection_ids': [123],
                          'inventory_available': True}]}

    def test_uses_storefront_label_instead_of_calculated_percentage(self):
        product, = scraper.extract_products(self.cards(), 'women')
        self.assertEqual(product['discount'], '65% off')  # 19 -> 7 is 63.16%, labeled 65%.
        self.assertEqual(product['currency'], 'USD')
        self.assertEqual(product['type'], 'women')
        self.assertEqual(product['original_price'], 19)
        self.assertEqual(product['discounted_price'], 7)
        self.assertEqual(product['name'], 'BEAMS JAPAN Toothbrush - White')
        self.assertTrue(product['url'].startswith('https://www.endclothing.com/en-cn/products/'))
        self.assertTrue(product['image_url'].startswith('https://'))

    def test_target_discounts_and_decimal_prices(self):
        for discount in ('60% off', '65% off', '70% off'):
            with self.subTest(discount=discount):
                html = CARD.replace('65% off', discount).replace('$19', '$1,019.50').replace('$7', '$407.80')
                product, = scraper.extract_products(self.cards(html))
                self.assertEqual(product['original_price'], 1019.5)
                self.assertEqual(product['discounted_price'], 407.8)
        self.assertEqual(scraper.extract_products(self.cards(CARD.replace('65% off', '55% off'))), [])

    def test_rejects_incomplete_target_data_and_wrong_currency(self):
        for html in (CARD.replace('class="prd-Card_Title"', ''),
                     CARD.replace('class="prd-Price_Discount"', ''),
                     CARD.replace('class="prd-Price_SalePrice"', ''),
                     CARD.replace('src=', 'data-src='),
                     CARD.replace('/en-cn/products/', '/en-gb/products/'),
                     CARD.replace('$', '£'), CARD.replace('$7', '$20')):
            with self.subTest(html=html):
                with self.assertRaises(ValueError):
                    scraper.extract_products(self.cards(html))

    def test_rejects_missing_or_duplicate_batch_cards(self):
        handle = self.cards()[0]['data-product-card-handle']
        with patch.object(scraper, 'request', return_value=self.response(CARD)):
            self.assertEqual(len(scraper.get_product_cards(None, [handle])), 1)
            with self.assertRaises(ValueError):
                scraper.get_product_cards(None, [handle, 'missing'])
        with patch.object(scraper, 'request', return_value=self.response(CARD + CARD)):
            with self.assertRaises(ValueError):
                scraper.get_product_cards(None, [handle, 'missing'])

    def test_pagination_rejects_truncated_wrong_page_or_wrong_collection(self):
        config = {'collection_id': '123'}
        self.assertEqual(scraper.get_page_info(self.info(), 1, config)['nbHits'], 1)
        for field, value in [('nbPages', 0), ('page', 1), ('hitsPerPage', 36),
                             ('nbHits', True), ('hits', [])]:
            info = self.info()
            info[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                scraper.get_page_info(info, 1, config)
        info = self.info()
        info['hits'][0]['collection_ids'] = [456]
        with self.assertRaises(ValueError):
            scraper.get_page_info(info, 1, config)
        info = self.info()
        info['nbHits'] = 2
        info['hits'] *= 2
        with self.assertRaises(ValueError):
            scraper.get_page_info(info, 1, config)

    def test_storefront_config_selects_china_index_and_actual_currency(self):
        config = {'app_id': 'TESTAPP', 'search_api_key': 'public-test-key',
                  'index_prefix': 'shopify_', 'markets_indexing_enabled': True,
                  'markets_mapping': [{'id': '42', 'locale': 'en', 'all_regions': ['CN'],
                                       'base_currency': 'USD', 'suffix': 'china_en'}]}
        html = '''<script>window.algoliaShopify.config = CONFIG;
        Shopify.country = "CN"; Shopify.locale = "en";
        Shopify.currency = {"active":"USD"};
        window.algoliaShopify.shopify.market = window.algoliaShopify.shopify.market || {"id":42};
        window.algoliaShopify.shopify.collection_id = 123;</script>'''.replace('CONFIG', json.dumps(config))
        with patch.object(scraper, 'request', return_value=self.response(html, scraper.BASE_URLS['men'])):
            result = scraper.get_storefront_config(None, 'men')
        self.assertEqual(result['index'], 'shopify_products_china_en')
        self.assertEqual(result['currency'], 'USD')
        self.assertEqual(result['collection_id'], '123')
        with patch.object(scraper, 'request', return_value=self.response(html.replace('"CN"', '"GB"'), scraper.BASE_URLS['men'])):
            with self.assertRaises(ValueError):
                scraper.get_storefront_config(None, 'men')

    def test_request_retries_transient_network_failure(self):
        session = Mock()
        session.request.side_effect = [scraper.requests.Timeout(), self.response('ok')]
        with patch.object(scraper.time, 'sleep'):
            self.assertEqual(scraper.request(session, 'GET', 'https://example.test').text, 'ok')
        self.assertEqual(session.request.call_count, 2)

    def test_connection_challenge_stops_without_retrying(self):
        response = self.response('<title>Verifying your connection...</title>')
        response.status_code = 429
        session = Mock()
        session.request.return_value = response
        with patch.object(scraper.time, 'sleep'), self.assertRaises(scraper.StorefrontVerificationRequired):
            scraper.request(session, 'GET', 'https://example.test')
        self.assertEqual(session.request.call_count, 1)

    def test_browser_initialization_timeout_is_retryable_with_context(self):
        driver = Mock(current_url=scraper.BASE_URLS['women'], title="Women's Sale – END.")
        driver.get.side_effect = scraper.TimeoutException()
        driver.execute_script.return_value = 'CN'
        with patch.object(scraper.webdriver, 'Chrome', return_value=driver):
            with scraper.BrowserSession('/test/chromedriver') as session:
                with self.assertRaisesRegex(scraper.requests.Timeout, 'womens-sale.*country=CN'):
                    session.request('GET', scraper.BASE_URLS['women'])
        driver.quit.assert_called_once()

    def test_rate_limit_honors_seconds_and_http_date(self):
        for header in ('90', format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))):
            with self.subTest(header=header):
                limited = self.response('Too many requests')
                limited.status_code = 429
                limited.headers = {'Retry-After': header}
                session = Mock()
                session.request.side_effect = [limited, self.response('ok')]
                with patch.object(scraper, 'next_request_at', 0), \
                        patch.object(scraper.time, 'monotonic', return_value=100), \
                        patch.object(scraper.time, 'sleep') as sleep, redirect_stdout(io.StringIO()):
                    self.assertEqual(scraper.request(session, 'GET', 'https://example.test').text, 'ok')
                self.assertGreaterEqual(sleep.call_args.args[0], 85)
                self.assertEqual(session.request.call_count, 2)

    def test_incomplete_migration_does_not_overwrite_existing_files(self):
        old = {('men', 'https://www.endclothing.com/cn/old.html'):
               {'type': 'men', 'url': 'https://www.endclothing.com/cn/old.html'}}
        with patch.object(scraper, 'load_existing_data', return_value=old), \
                patch.object(scraper, 'ChromeDriverManager'), \
                patch.object(scraper, 'scrape_type', return_value=({}, [1])), \
                patch.object(scraper, 'save_data') as save, redirect_stdout(io.StringIO()):
            self.assertEqual(scraper.main(), 1)
        save.assert_not_called()

    def test_complete_run_replaces_legacy_records(self):
        old = {('men', 'old'): {'type': 'men', 'url': 'old'}}
        def scrape(kind, driver_path):
            product, = scraper.extract_products(self.cards(), kind)
            return {(kind, product['url']): product}, []
        with patch.object(scraper, 'load_existing_data', return_value=copy.deepcopy(old)), \
                patch.object(scraper, 'ChromeDriverManager'), \
                patch.object(scraper, 'scrape_type', side_effect=scrape), \
                patch.object(scraper, 'save_data') as save, \
                patch.dict(scraper.os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(scraper.main(), 0)
        saved, = save.call_args.args
        self.assertEqual(len(saved), 2)
        self.assertNotIn(('men', 'old'), saved)


if __name__ == '__main__':
    unittest.main()
