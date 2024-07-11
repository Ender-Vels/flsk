import math
from flask import Flask, render_template, request, jsonify
import threading
import json
import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from bs4 import BeautifulSoup
from binance.client import Client
import time
import re
import redis
import logging


app = Flask(__name__)

running_scrapers = {}


class ScrapeTask:
    def __init__(self, task_id, link, api_key, api_secret, leverage, trader_portfolio_size, your_portfolio_size):
        self.link = link
        self.task_id = task_id
        self.driver = None
        self.binance_client = None
        self.processed_orders = set()
        self.current_page = 1
        self.current_time = None
        self.all_orders = []
        self.timer = None
        self.running = False
        self.leverage = leverage
        self.trader_portfolio_size = trader_portfolio_size
        self.your_portfolio_size = your_portfolio_size
        self.close_only_mode = False
        self.reverse_copy = False
        self.api_key = api_key
        self.api_secret = api_secret
        self.min_order_quantity = {}
        self.initialize_binance_client()


    def stop(self):

        if self.running:
            self.running = False
            if self.driver:
                self.driver.quit()
            if self.timer:
                self.timer.cancel()
            print(f"Scraper {self.task_id} stopped.")
        else:
            print(f"Scraper {self.task_id} is not running.")

    def initialize_driver(self):
        try:
            chrome_options = Options()
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-dev-shm-usage")
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.get(self.link)
            print("WebDriver initialized.")
        except Exception as e:
            print(f"Error initializing WebDriver: {e}")
            self.running = False

    def initialize_binance_client(self):
        try:
            self.binance_client = Client(self.api_key, self.api_secret)
            print("Binance client initialized.")
        except Exception as e:
            print(f"Error initializing Binance client: {e}")
            self.running = False

    def start_scraping(self, close_only_mode=False, reverse_copy=False):
        self.close_only_mode = close_only_mode
        self.reverse_copy = reverse_copy
        if not self.driver:
            self.initialize_driver()
            self.accept_cookies()
            self.navigate_to_trade_history()

        self.running = True
        self.scrape_and_display_orders()

    def accept_cookies(self):
        try:
            time.sleep(2)
            accept_btn = self.find_element_with_retry(By.ID, "onetrust-accept-btn-handler")
            accept_btn.click()
            print("Accepted cookies.")
            time.sleep(2)
        except Exception as e:
            print(f"Error accepting cookies: {e}")

    def navigate_to_trade_history(self):
        try:
            move_to_trade_history = self.find_element_with_retry(By.CSS_SELECTOR, "#tab-tradeHistory > div")
            self.driver.execute_script("arguments[0].scrollIntoView(true);", move_to_trade_history)
            move_to_trade_history.click()
            print("Navigated to trade history tab.")
            time.sleep(2)
        except Exception as e:
            print(f"Trade history tab not found: {e}")
            self.driver.refresh()
            print("Page refreshed.")
            self.navigate_to_trade_history()

    def scrape_and_display_orders(self):
        try:
            while self.running:
                self.current_time = datetime.datetime.now().replace(second=0, microsecond=0)
                print(f"Current time: {self.current_time}")

                found_data = False
                soup = BeautifulSoup(self.driver.page_source, 'html.parser')
                orders = soup.select(".css-g5h8k8 > div > div > div > table > tbody > tr")

                for order in orders:
                    time_str = order.select_one("td:nth-child(1)").text.strip()
                    order_time = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S').replace(second=0, microsecond=0)
                    time_diff = (self.current_time - order_time).total_seconds() / 60

                    if abs(time_diff) <= 2:
                        symbol = order.select_one("td:nth-child(2)").text.strip()
                        side = order.select_one("td:nth-child(3)").text.strip()
                        price_str = order.select_one("td:nth-child(4)").text.strip()
                        quantity_str = order.select_one("td:nth-child(5)").text.strip()
                        realized_profit_str = order.select_one("td:nth-child(6)").text.strip()

                        price = float(re.sub(r'[^\d.]', '', price_str.replace(',', '')))
                        quantity_str = quantity_str.split(' ', 1)[0].replace(',', '')
                        quantity = float(quantity_str)
                        realized_profit_str = realized_profit_str.replace('USDT', '').strip()
                        realized_profit = float(realized_profit_str.replace(',', ''))

                        symbol = self.add_space_before_and_remove_perpetual(symbol)

                        order_id = f"{time_str}-{symbol}-{side}-{price}"
                        if order_id not in self.processed_orders:
                            self.processed_orders.add(order_id)
                            order_data = {
                                "Time": time_str,
                                "Symbol": symbol,
                                "Side": side,
                                "Price": price,
                                "Quantity": quantity,
                                "Realized Profit": realized_profit
                            }
                            self.all_orders.append(order_data)
                            found_data = True
                            print(f"Added order: {order_id}")
                            self.exec_order(symbol, side, quantity, realized_profit)

                if not found_data:
                    print("No data found on current page.")
                    self.go_to_first_page()
                    continue

                next_page_button = self.find_element_with_retry(By.CSS_SELECTOR, "div.bn-pagination-next")
                self.driver.execute_script("arguments[0].scrollIntoView(true);", next_page_button)
                next_page_button.click()
                print("Navigated to next page.")
                time.sleep(2)
                self.current_page += 1

                if not self.has_next_page():
                    print("No next page found. Returning to first page.")
                    self.go_to_first_page()
                    time.sleep(2)

                self.save_orders_to_file()

        except Exception as e:
            print(f"Error scraping and displaying orders: {e}")
        finally:
            if self.driver:
                self.initialize_driver()
                self.accept_cookies()
                self.navigate_to_trade_history()
                self.running = True
                self.scrape_and_display_orders()
            else:
                print("Scraping has been stopped.")

    def exec_order(self, symbol, side, quantity, realized_profit):
        client = self.binance_client
        if side in ['Open Long', 'Buy/Long'] and realized_profit == 0.0:
            quantity = float(quantity)
            quantity = (quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)
            minQt = client.get_symbol_info(symbol)
            minQuantity = float(minQt['filters'][6]['minNotional'])
            stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
            quantity_precision = int(round(-math.log(stepSize, 10)))
            quantity = round(quantity, quantity_precision)
            side = 'BUY'
            position_side = 'LONG'
            if quantity < minQuantity:
                quantity = minQuantity
            try:
                client.futures_create_order(symbol=symbol,
                                            side=side,
                                            positionSide=position_side,
                                            type='MARKET',
                                            leverage=int(self.leverage),
                                            quantity=quantity)
                print(f"Executed order: {symbol} {side} {quantity}")
            except Exception as e:
                print(f"Error executing order: {e}")

        elif side in ['Close Long', 'Sell/Short'] and realized_profit != 0.0:
            quantity = float(quantity)
            quantity = ((quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)) * 1.05
            minQt = client.get_symbol_info(symbol)
            minQuantity = float(minQt['filters'][6]['minNotional'])
            stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
            quantity_precision = int(round(-math.log(stepSize, 10)))
            quantity = round(quantity, quantity_precision)
            side = 'SELL'
            position_side = 'LONG'
            if quantity < minQuantity:
                quantity = minQuantity
            try:
                client.futures_create_order(symbol=symbol,
                                            side=side,
                                            positionSide=position_side,
                                            type='MARKET',
                                            leverage=int(self.leverage),
                                            quantity=quantity)
                print(f"Executed order: {symbol} {side} {quantity}")
            except Exception as e:
                print(f"Error executing order: {e}")

        elif side in ['Open Short', 'Sell/Short'] and realized_profit == 0.0:
            side = 'SELL'
            position_side = 'SHORT'
            quantity = float(quantity)
            quantity = (quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)
            minQt = client.get_symbol_info(symbol)
            minQuantity = float(minQt['filters'][6]['minNotional'])
            stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
            quantity_precision = int(round(-math.log(stepSize, 10)))
            quantity = round(quantity, quantity_precision)
            if quantity < minQuantity:
                quantity = minQuantity
            try:
                client.futures_create_order(symbol=symbol,
                                            side=side,
                                            positionSide=position_side,
                                            type='MARKET',
                                            leverage=int(self.leverage),
                                            quantity=quantity)
                print(f"Executed order: {symbol} {side} {quantity}")
            except Exception as e:
                print(f"Error executing order: {e}")

        elif side in ['Close Short', 'Buy/Long'] and realized_profit != 0.0:
            quantity = float(quantity)
            quantity = ((quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)) * 1.05
            minQt = client.get_symbol_info(symbol)
            minQuantity = float(minQt['filters'][6]['minNotional'])
            stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
            quantity_precision = int(round(-math.log(stepSize, 10)))
            quantity = round(quantity, quantity_precision)
            side = 'BUY'
            position_side = 'SHORT'
            if quantity < minQuantity:
                quantity = minQuantity
            try:
                client.futures_create_order(symbol=symbol,
                                            side=side,
                                            positionSide=position_side,
                                            type='MARKET',
                                            leverage=int(self.leverage),
                                            quantity=quantity)
                print(f"Executed order: {symbol} {side} {quantity}")
            except Exception as e:
                print(f"Error executing order: {e}")
        if self.close_only_mode:
            if side in ['Open Long', 'Buy/Long'] and realized_profit == 0.0:
                return  # Ignore this order in close only mode
            if side in ['Close Long', 'Sell/Short'] and realized_profit != 0.0:
                return  # Ignore this order in close only mode
            
        if self.reverse_copy:
            if side in ['Open Long', 'Buy/Long'] and realized_profit == 0.0:
                side = 'SELL'
                position_side = 'SHORT'
                quantity = float(quantity)
                quantity = ((quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)) * 1.05
                minQt = client.get_symbol_info(symbol)
                minQuantity = float(minQt['filters'][6]['minNotional'])
                stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
                quantity_precision = int(round(-math.log(stepSize, 10)))
                quantity = round(quantity, quantity_precision)
                if quantity < minQuantity:
                    quantity = minQuantity
                try:
                    client.futures_create_order(symbol=symbol,
                                                side=side,
                                                positionSide=position_side,
                                                type='MARKET',
                                                leverage=int(self.leverage),
                                                quantity=quantity)
                    print(f"Executed order: {symbol} {side} {quantity}")
                except Exception as e:
                    print(f"Error executing order: {e}")

            elif side in ['Close Long', 'Sell/Short'] and realized_profit != 0.0:
                side = 'BUY'
                position_side = 'SHORT'
                quantity = float(quantity)
                quantity = (quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)
                minQt = client.get_symbol_info(symbol)
                minQuantity = float(minQt['filters'][6]['minNotional'])
                stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
                quantity_precision = int(round(-math.log(stepSize, 10)))
                quantity = round(quantity, quantity_precision)
                if quantity < minQuantity:
                    quantity = minQuantity
                try:
                    client.futures_create_order(symbol=symbol,
                                                side=side,
                                                positionSide=position_side,
                                                type='MARKET',
                                                leverage=int(self.leverage),
                                                quantity=quantity)
                    print(f"Executed order: {symbol} {side} {quantity}")
                except Exception as e:
                    print(f"Error executing order: {e}")

            elif side in ['Open Short', 'Buy/Long'] and realized_profit == 0.0:
                side = 'BUY'
                position_side = 'LONG'
                quantity = float(quantity)
                quantity = ((quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)) * 1.05
                minQt = client.get_symbol_info(symbol)
                minQuantity = float(minQt['filters'][6]['minNotional'])
                stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
                quantity_precision = int(round(-math.log(stepSize, 10)))
                quantity = round(quantity, quantity_precision)
                if quantity < minQuantity:
                    quantity = minQuantity
                try:
                    client.futures_create_order(symbol=symbol,
                                                side=side,
                                                positionSide=position_side,
                                                type='MARKET',
                                                leverage=int(self.leverage),
                                                quantity=quantity)
                    print(f"Executed order: {symbol} {side} {quantity}")
                except Exception as e:
                    print(f"Error executing order: {e}")
            elif side in ['Close Short', 'Buy/Long'] and realized_profit != 0.0:
                side = 'SELL'
                position_side = 'LONG'
                quantity = float(quantity)
                quantity = (quantity * float(self.your_portfolio_size)) / float(self.trader_portfolio_size)
                minQt = client.get_symbol_info(symbol)
                minQuantity = float(minQt['filters'][6]['minNotional'])
                stepSize = float(next(f['stepSize'] for f in minQt['filters'] if f['filterType'] == 'LOT_SIZE'))
                quantity_precision = int(round(-math.log(stepSize, 10)))
                quantity = round(quantity, quantity_precision)
                if quantity < minQuantity:
                    quantity = minQuantity
                try:
                    client.futures_create_order(symbol=symbol,
                                                side=side,
                                                positionSide=position_side,
                                                type='MARKET',
                                                leverage=int(self.leverage),
                                                quantity=quantity)
                    print(f"Executed order: {symbol} {side} {quantity}")
                except Exception as e:
                    print(f"Error executing order: {e}")

    def find_element_with_retry(self, by, selector, max_attempts=3):
        attempts = 0
        while attempts < max_attempts:
            try:
                element = self.driver.find_element(by, selector)
                return element
            except Exception as e:
                attempts += 1
                print(f"Error finding element {selector} (Attempt {attempts}/{max_attempts}): {e}")
                time.sleep(2)
        return None

    def has_next_page(self):
        try:
            next_page_button = self.driver.find_element(By.CSS_SELECTOR, "div.bn-pagination-next")
            return next_page_button.is_enabled()
        except NoSuchElementException:
            return False

    def go_to_first_page(self):
        try:
            self.driver.get(self.link)
            time.sleep(2)
            self.navigate_to_trade_history()
            self.current_page = 1
        except Exception as e:
            print(f"Error navigating to first page: {e}")

    def add_space_before_and_remove_perpetual(self, text):
        text = re.sub(r" ?Perpetual", "", text)
        return text.strip()

    def save_orders_to_file(self):
        summarized_orders = self.summarize_orders(self.all_orders)
        with open('trade_history.json', 'w') as json_file:
            json.dump(summarized_orders, json_file, indent=4)
        print("Orders saved to file.")
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(300, self.delete_orders_from_file)
        self.timer.start()

    def delete_orders_from_file(self):
        self.all_orders.clear()
        with open('trade_history.json', 'w') as json_file:
            json.dump(self.all_orders, json_file, indent=4)
        print("Orders deleted from file after 5 minutes.")

    def summarize_orders(self, orders):
        summarized_orders = {}
        for order in orders:
            key = f"{order['Symbol']}_{order['Side']}_{order['Price']}"
            if key in summarized_orders:
                summarized_orders[key]['Quantity'] += order['Quantity']
                summarized_orders[key]['Realized Profit'] += order['Realized Profit']
            else:
                summarized_orders[key] = {
                    "Time": order['Time'],
                    "Symbol": order['Symbol'],
                    "Side": order['Side'],
                    "Price": order['Price'],
                    "Quantity": order['Quantity'],
                    "Realized Profit": order['Realized Profit']
                }
        return list(summarized_orders.values())


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_scrape():
    form_data = request.json
    task_id = form_data['task_id']  # Отримати task_id з JSON даних
    print(task_id)
    link = form_data['link']
    api_key = form_data['api_key']
    api_secret = form_data['api_secret']
    leverage = form_data['leverage']
    trader_portfolio_size = form_data['trader_portfolio_size']
    your_portfolio_size = form_data['your_portfolio_size']
    close_only_mode = False
    reverse_copy = False

    if task_id not in running_scrapers:
        scraper_task = ScrapeTask(task_id, link, api_key, api_secret, leverage, trader_portfolio_size, your_portfolio_size)
        running_scrapers[task_id] = scraper_task
        threading.Thread(target=scraper_task.start_scraping, args=(close_only_mode, reverse_copy)).start()
        return jsonify({'task_id':task_id})
    else:
        return jsonify({'status': 'error', 'message': f'Scraper {task_id} is already running.'})



@app.route('/running', methods=['GET'])
def list_running_scrapers():
    running_tasks = [{'task_id': task_id, 'link': scraper_task.link, 'running': scraper_task.running} for task_id, scraper_task in running_scrapers.items()]
    return jsonify(running_tasks)


@app.route('/stop', methods=['POST'])
def stop_scraping():
    if request.method == 'POST':
        data = request.json  # Extract JSON data from request
        task_id = data.get('task_id')

        if task_id in running_scrapers:
            scraper_task = running_scrapers[task_id]
            scraper_task.stop()  # Implement stop_scraping method in ScrapeTask class
            del running_scrapers[task_id]
            return jsonify({'status': 'success', 'message': f'Scraper {task_id} stopped.'}), 200
        else:
            return jsonify({'status': 'error', 'message': f'Scraper {task_id} is not running.'}), 404



if __name__ == '__main__':
    app.run(debug=True)
