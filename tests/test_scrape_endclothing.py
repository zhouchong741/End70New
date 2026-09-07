import json
import math
import os
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape_endclothing as scraper


def product(product_type, url="https://www.endclothing.com/cn/shared.html"):
    return {
        "type": product_type,
        "url": url,
        "name": product_type,
        "original_price": 1000,
        "discounted_price": 300,
        "discount": "70% off",
        "image_url": "https://images.example.com/product.jpg",
    }


def page_fixture(total=3, page_size=2, page=1, product_type="men", card_count=None, country="cn"):
    count = min(page_size, max(0, total - (page - 1) * page_size))
    info = {
        "nbHits": total,
        "nbPages": math.ceil(total / page_size),
        "page": page - 1,
        "hitsPerPage": page_size,
        "hits": [{"objectID": str(index), "sale_percentage": "45%"} for index in range(count)],
    }
    data = {
        "query": {
            "countryCode": country,
            "route": (["women"] if product_type == "women" else []) + ["sale", "all-sale"],
        },
        "props": {"initialProps": {"pageProps": {"initialAlgoliaState": {"results": info}}}},
    }
    cards = "".join(
        f'<a href="/cn/product-{index}.html" data-test-id="ProductCard__ProductCardSC"></a>'
        for index in range(count if card_count is None else card_count)
    )
    html = (
        '<a href="/cn/unrelated-recommendation.html">Recommendation</a>'
        f'<div id="plpBody">{cards}</div>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    )
    return scraper.BeautifulSoup(html, "html.parser")


class ScraperMainTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {"GITHUB_ENV": ""}))
        self.patches.enter_context(patch("builtins.print"))
        self.chrome = self.patches.enter_context(patch.object(scraper.webdriver, "Chrome"))
        self.manager = self.patches.enter_context(patch.object(scraper, "ChromeDriverManager"))
        self.manager.return_value.install.return_value = "/mock/chromedriver"
        self.existing = self.patches.enter_context(patch.object(scraper, "load_existing_data"))
        old_product = product("men", "https://www.endclothing.com/cn/old.html")
        self.existing.return_value = {("men", old_product["url"]): old_product}
        self.save = self.patches.enter_context(patch.object(scraper, "save_data"))
        self.worker = self.patches.enter_context(patch.object(scraper, "scrape_type"))

    def test_both_types_overlap_and_save_once_in_type_order(self):
        overlap = threading.Barrier(2, timeout=5)
        threads = {}

        def scrape_type(product_type, driver_path):
            self.assertEqual(driver_path, "/mock/chromedriver")
            threads[product_type] = threading.get_ident()
            overlap.wait()
            item = product(product_type)
            return {(product_type, item["url"]): item}

        self.worker.side_effect = scrape_type

        # Collect Women first deliberately; output must still follow BASE_URLS order.
        with patch.object(scraper, "as_completed", side_effect=lambda futures: reversed(list(futures))):
            self.assertEqual(scraper.main(), 0)

        self.assertEqual(set(threads), {"men", "women"})
        self.assertNotEqual(threads["men"], threads["women"])
        self.manager.assert_called_once_with()
        self.manager.return_value.install.assert_called_once_with()
        self.save.assert_called_once()
        saved = self.save.call_args.args[0]
        self.assertEqual(list(saved), [(kind, product(kind)["url"]) for kind in scraper.BASE_URLS])
        self.assertEqual([item["type"] for item in saved.values()], ["men", "women"])
        self.assertEqual(len(saved), 2, "The same URL in both types must remain separate")
        self.chrome.assert_not_called()

    def test_one_failed_type_does_not_save_partial_results(self):
        def scrape_type(product_type, driver_path):
            if product_type == "women":
                raise RuntimeError("Women page 2 failed")
            item = product(product_type)
            return {(product_type, item["url"]): item}

        self.worker.side_effect = scrape_type

        self.assertNotEqual(scraper.main(), 0)

        self.save.assert_not_called()
        self.assertEqual(len(self.existing.return_value), 1)
        self.assertIn(("men", "https://www.endclothing.com/cn/old.html"), self.existing.return_value)

    def test_driver_install_failure_does_not_start_workers_or_save(self):
        self.manager.return_value.install.side_effect = RuntimeError("Driver download failed")

        self.assertNotEqual(scraper.main(), 0)

        self.worker.assert_not_called()
        self.chrome.assert_not_called()
        self.save.assert_not_called()


class ScrapeTypeTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch("builtins.print"))
        self.patches.enter_context(patch.object(scraper.time, "sleep"))
        self.chrome = self.patches.enter_context(patch.object(scraper.webdriver, "Chrome"))
        self.service = self.patches.enter_context(patch.object(scraper, "Service"))
        self.manager = self.patches.enter_context(patch.object(scraper, "ChromeDriverManager"))
        self.get_soup = self.patches.enter_context(patch.object(scraper, "get_page_soup"))
        self.get_info = self.patches.enter_context(patch.object(scraper, "get_page_info"))
        self.extract = self.patches.enter_context(patch.object(scraper, "extract_products"))
        self.get_soup.return_value = sentinel.soup
        self.get_info.return_value = {
            "nbHits": 1,
            "nbPages": 1,
            "page": 0,
            "hitsPerPage": 120,
            "hits": [{"url": "/cn/shared.html", "sale_percentage": "70%"}],
        }

    def test_successful_types_use_and_close_independent_drivers(self):
        men_driver, women_driver = Mock(name="men_driver"), Mock(name="women_driver")
        self.chrome.side_effect = [men_driver, women_driver]
        self.extract.side_effect = lambda soup, kind: [product(kind)]

        men_result = scraper.scrape_type("men", "/mock/chromedriver")
        women_result = scraper.scrape_type("women", "/mock/chromedriver")

        self.assertEqual(men_result, {("men", product("men")["url"]): product("men")})
        self.assertEqual(women_result, {("women", product("women")["url"]): product("women")})
        self.get_soup.assert_any_call(1, men_driver, "men")
        self.get_soup.assert_any_call(1, women_driver, "women")
        self.assertEqual(self.chrome.call_count, 2)
        men_driver.quit.assert_called_once_with()
        women_driver.quit.assert_called_once_with()
        self.manager.assert_not_called()

    def test_later_page_failure_raises_and_closes_driver(self):
        self.get_info.return_value.update(nbHits=2, nbPages=2, hitsPerPage=1)
        self.get_soup.side_effect = [sentinel.soup, RuntimeError("Page 2 failed")]
        self.extract.return_value = [product("men")]

        with self.assertRaisesRegex(RuntimeError, "Page 2 failed"):
            scraper.scrape_type("men", "/mock/chromedriver")

        self.get_soup.assert_any_call(2, self.chrome.return_value, "men")
        self.chrome.return_value.quit.assert_called_once_with()

    def test_first_page_failure_closes_driver(self):
        self.get_soup.side_effect = RuntimeError("First page failed")

        with self.assertRaisesRegex(RuntimeError, "First page failed"):
            scraper.scrape_type("women", "/mock/chromedriver")

        self.chrome.return_value.quit.assert_called_once_with()
        self.extract.assert_not_called()

    def test_incomplete_matching_product_raises_and_closes_driver(self):
        incomplete = product("men")
        del incomplete["discounted_price"]
        self.extract.return_value = [incomplete]

        with self.assertRaisesRegex(ValueError, "Incomplete product data"):
            scraper.scrape_type("men", "/mock/chromedriver")

        self.chrome.return_value.quit.assert_called_once_with()

    def test_empty_type_returns_empty_results_and_closes_driver(self):
        self.get_info.return_value.update(nbHits=0, nbPages=0, hits=[])

        self.assertEqual(scraper.scrape_type("women", "/mock/chromedriver"), {})

        self.extract.assert_not_called()
        self.chrome.return_value.quit.assert_called_once_with()

    def test_missing_expected_discount_product_raises_and_closes_driver(self):
        self.extract.return_value = []

        with self.assertRaises(ValueError):
            scraper.scrape_type("men", "/mock/chromedriver")

        self.chrome.return_value.quit.assert_called_once_with()


class GetPageSoupTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch("builtins.print"))
        self.sleep = self.patches.enter_context(patch.object(scraper.time, "sleep"))
        self.wait = self.patches.enter_context(patch.object(scraper, "WebDriverWait"))
        self.driver = Mock()
        self.driver.page_source = str(page_fixture())

    def test_ready_page_does_not_use_fixed_sleep(self):
        soup = scraper.get_page_soup(1, self.driver, "men")

        self.assertEqual(scraper.get_page_info(soup, "men", 1)["nbHits"], 3)
        self.wait.return_value.until.assert_called_once()
        self.sleep.assert_not_called()
        self.driver.quit.assert_not_called()

    def test_transient_load_failure_is_retried(self):
        self.driver.get.side_effect = [RuntimeError("Temporary loading error"), None]

        soup = scraper.get_page_soup(1, self.driver, "men")

        self.assertEqual(scraper.get_page_info(soup, "men", 1)["nbHits"], 3)
        self.assertEqual(self.driver.get.call_count, 2)
        self.sleep.assert_called_once_with(scraper.PAGE_INTERVAL)

    def test_exhausted_retries_raise_instead_of_returning_empty_page(self):
        self.driver.get.side_effect = RuntimeError("Page load failed")

        with self.assertRaisesRegex(RuntimeError, "failed after retries"):
            scraper.get_page_soup(1, self.driver, "men")

        self.assertEqual(self.driver.get.call_count, scraper.PAGE_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, scraper.PAGE_ATTEMPTS - 1)
        self.driver.quit.assert_not_called()


class PageInfoTests(unittest.TestCase):
    def test_authoritative_pages_ignore_unrelated_global_product_links(self):
        soup = page_fixture(total=5765, page_size=120)

        info = scraper.get_page_info(soup, "men", 1)

        self.assertEqual(len(soup.find_all("a")), 121)
        self.assertEqual(info["nbPages"], 49)
        self.assertEqual(info["hitsPerPage"], 120)

    def test_wrong_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "page number"):
            scraper.get_page_info(page_fixture(), "men", 2)

    def test_wrong_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "country or product type"):
            scraper.get_page_info(page_fixture(), "women", 1)

    def test_wrong_country_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "country or product type"):
            scraper.get_page_info(page_fixture(country="us"), "men", 1)

    def test_incomplete_cards_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Incomplete product list"):
            scraper.get_page_info(page_fixture(card_count=1), "men", 1)

    def test_expected_discount_missing_from_dom_is_rejected(self):
        soup = page_fixture()
        script = soup.find("script", id="__NEXT_DATA__")
        data = json.loads(script.string)
        data["props"]["initialProps"]["pageProps"]["initialAlgoliaState"]["results"]["hits"][0][
            "sale_percentage"
        ] = "70%"
        script.string = json.dumps(data)

        with self.assertRaises(ValueError):
            scraper.get_page_info(soup, "men", 1)

    def test_missing_structured_discount_is_rejected(self):
        soup = page_fixture()
        script = soup.find("script", id="__NEXT_DATA__")
        data = json.loads(script.string)
        del data["props"]["initialProps"]["pageProps"]["initialAlgoliaState"]["results"]["hits"][0][
            "sale_percentage"
        ]
        script.string = json.dumps(data)

        with self.assertRaises(ValueError):
            scraper.get_page_info(soup, "men", 1)

    def test_valid_empty_type_is_accepted(self):
        info = scraper.get_page_info(page_fixture(total=0, product_type="women"), "women", 1)

        self.assertEqual(info["nbHits"], 0)
        self.assertEqual(info["nbPages"], 0)
        self.assertEqual(info["hits"], [])

    def test_partial_last_page_is_accepted(self):
        info = scraper.get_page_info(page_fixture(page=2), "men", 2)

        self.assertEqual(info["page"], 1)
        self.assertEqual(len(info["hits"]), 1)


class ExtractProductsTests(unittest.TestCase):
    def test_srcset_image_url_preserves_embedded_commas(self):
        image_url = "https://images.example.com/f_auto,q_auto:eco,w_640/product.jpg"
        soup = scraper.BeautifulSoup(
            '<div id="plpBody"><a href="/cn/shared.html" data-test-id="ProductCard__ProductCardSC">'
            f'<img srcset="{image_url} 640w, https://images.example.com/large.jpg 1280w">'
            '<span>Test product</span><span>CN¥1,000</span><span>CN¥300</span>'
            '<span>70% off</span></a></div>',
            "html.parser",
        )

        products = scraper.extract_products(soup, "women")

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["image_url"], image_url)
        self.assertEqual(products[0]["type"], "women")
        self.assertEqual(products[0]["original_price"], 1000)
        self.assertEqual(products[0]["discounted_price"], 300)


if __name__ == "__main__":
    unittest.main()
