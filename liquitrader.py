import asyncio
import os
import sys
import json
import time
import traceback
import threading

import verifier

from config import Config
from exchanges import BinanceExchange
from exchanges import GenericExchange
from utils.DepthAnalyzer import *

from exchanges import PaperBinance
from analyzers.TechnicalAnalysis import run_ta
from conditions.BuyCondition import BuyCondition
from conditions.DCABuyCondition import DCABuyCondition
from conditions.SellCondition import SellCondition
from utils.Utils import *
from conditions.condition_tools import percentToFloat


from dev_keys_binance import keys  # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

if hasattr(sys, 'frozen'):
    os.environ["REQUESTS_CA_BUNDLE"] = os.path.join(os.path.dirname(sys.executable), 'lib', 'cacert.pem')

DEFAULT_COLUMNS = ['last_order_time', 'symbol', 'avg_price', 'close', 'gain', 'quoteVolume', 'total_cost', 'current_value', 'dca_level', 'total', 'percentage']

# FRIENDLY_HOLDING_COLUMNS =  ['Last Purchase Time', 'Symbol', 'Price', 'Bought Price', '% Change', 'Volume',
#                              'Bought Value', 'Current Value', 'DCA Level', 'Amount', '24h Change']
COLUMN_ALIASES = {'last_order_time': 'Last Purchase Time',
                 'symbol': 'Symbol',
                 'avg_price': 'Bought Price',
                 'close': 'Price',
                 'gain': '% Change',
                 'quoteVolume': 'Volume',
                 'total_cost': 'Bought Value',
                 'current_value': 'Current Value',
                 'dca_level': 'DCA Level',
                 'total': 'Amount',
                 'percentage': '24h Change'
                  }


FRIENDLY_MARKET_COLUMNS = ['Symbol', 'Price', 'Volume',
                           'Amount', '24h Change']


class User:
    balance = 5


user = User()


class LiquiTrader:
    """
    Needs:
        - self.exchange
        - Config
        - Buy/sell/dca Strategies

    functions:
        - Analyze buy strategies
        - analyze sell strategies
        - analyze dca strategies

        - Handle possible sells
        - Handle buys
        - handle dca buys

        - get active config
        - update config
        - update strategies
    """

    def __init__(self):
        self.exchange = None
        self.statistics = {}
        self.config = None
        self.buy_strategies = None
        self.sell_strategies = None
        self.dca_buy_strategies = None
        self.trade_history = []
        self.indicators = None
        self.timeframes = None
        self.owned = []

    # ----
    def initialize_config(self):
        self.config = Config()
        self.config.load_general_settings()
        self.config.load_global_trade_conditions()
        self.indicators = self.config.get_indicators()
        self.timeframes = self.config.timeframes

    # ----
    def initialize_exchange(self):
        if self.config.general_settings['exchange'].lower() == 'binance' and self.config.general_settings['paper_trading']:
            self.exchange = PaperBinance.PaperBinance('binance',
                                                      self.config.general_settings['market'].upper(),
                                                      self.config.general_settings['starting_balance'],
                                                      {'public': keys.public, 'secret': keys.secret},
                                                      self.timeframes)

            # use USDT in tests to decrease API calls (only ~12 pairs vs 100+)
        elif self.config.general_settings['exchange'].lower() == 'binance':
            self.exchange = BinanceExchange.BinanceExchange('binance',
                                                            self.config.general_settings['market'].upper(),
                                                            {'public': keys.public, 'secret': keys.secret},
                                                            self.timeframes)

        else:
            self.exchange = GenericExchange.GenericExchange(self.config.general_settings['exchange'].lower(),
                                                            self.config.general_settings['market'].upper(),
                                                            {'public': keys.public, 'secret': keys.secret},
                                                            self.timeframes)

        asyncio.get_event_loop().run_until_complete(self.exchange.initialize())

    # ----
    # return total current value (pairs + balance)
    def get_tcv(self):
        pending = 0
        self.owned = []
        for pair, value in self.exchange.pairs.items():
            if 'total' not in value or 'close' not in value: continue
            pending += value['close'] * value['total']
            if value['close'] * value['total'] > 0:
                self.owned.append(pair)
        return pending + self.exchange.balance

    # ----
    def load_strategies(self):
        # TODO get candle periods and indicators here or in load config
        # instantiate strategies
        buy_strategies = []
        for strategy in self.config.buy_strategies:
            buy_strategies.append(BuyCondition(strategy))

        dca_buy_strategies = []
        for strategy in self.config.dca_buy_strategies:
            dca_buy_strategies.append(DCABuyCondition(strategy))

        sell_strategies = []
        for strategy in self.config.sell_strategies:
            sell_strategies.append(SellCondition(strategy))

        self.buy_strategies = buy_strategies
        self.sell_strategies = sell_strategies
        self.dca_buy_strategies = dca_buy_strategies

    # ----
    def get_possible_buys(self, pairs, strategies):
        possible_trades = {}
        tcv = self.get_tcv()
        for strategy in strategies:
            for pair in pairs:
                # strategy.evaluate(pairs[pair],statistics[pair])
                try:
                    result = strategy.evaluate(pairs[pair], self.statistics[pair], tcv)

                except Exception as ex:
                    print('exception in get possible buys: {}'.format(traceback.format_exc()))
                    self.exchange.reload_single_candle_history(pair)
                    continue

                if result is not None:
                    if pair not in possible_trades or possible_trades[pair] > result:
                        possible_trades[pair] = result

            return possible_trades

    # ----
    def get_possible_sells(self, pairs, strategies):
        possible_trades = {}
        for strategy in strategies:
            for pair in pairs:
                # strategy.evaluate(pairs[pair],statistics[pair])
                result = strategy.evaluate(pairs[pair], self.statistics[pair])

                if result is not None:
                    if pair not in possible_trades or possible_trades[pair] < result:
                        possible_trades[pair] = result

            return possible_trades

    # ----
    @staticmethod
    def check_for_viable_trade(current_price, orderbook, remaining_amount, min_cost, max_spread, dca=False):
        can_fill, minimum_fill = process_depth(orderbook, remaining_amount, min_cost)

        if can_fill is not None and in_max_spread(current_price, can_fill.price, max_spread):
            return can_fill

        elif minimum_fill is not None and in_max_spread(current_price, minimum_fill.price, max_spread) and not dca:
            return minimum_fill

        else:
            return None

    # ----
    # check min balance, max pairs, quote change, market change, trading enabled, blacklist, whitelist, 24h change
    # todo add pair specific settings
    def handle_possible_buys(self, possible_buys):
        # Alleviate lookup cost
        exchange = self.exchange
        config = self.config
        exchange_pairs = exchange.pairs

        for pair in possible_buys:
            exch_pair = exchange_pairs[pair]

            if self.pair_specific_buy_checks(pair, exch_pair['close'], possible_buys[pair],
                                             exchange.balance, exch_pair['percentage'],
                                             config.global_trade_conditions['min_buy_balance']):

                # amount we'd like to own
                target_amount = possible_buys[pair]
                # difference between target and current owned quantity.
                remaining_amount = target_amount - exch_pair['total']
                # lowest cost trade-able
                min_cost = exchange.get_min_cost(pair)
                current_price = exch_pair['close']
    
                # get orderbook, if time since last orderbook check is too soon, it will return none
                orderbook = exchange.get_depth(pair, 'BUY')
                if orderbook is None:
                    continue
    
                # get viable trade, returns None if none available
                price_info = self.check_for_viable_trade(current_price, orderbook, remaining_amount, min_cost,
                                                         config.global_trade_conditions['max_spread'])
    
                # Check to see if amount remaining to buy is greater than min trade quantity for pair
                if price_info is None or price_info.amount * price_info.average_price < min_cost:
                    continue
    
                # place order
                order = exchange.place_order(pair, 'limit', 'buy', price_info.amount, price_info.price)
                # store order in trade history
                self.trade_history.append(order)
                self.save_trade_history()

    # ----
    def handle_possible_sells(self, possible_sells):
        # Alleviate lookup cost
        exchange = self.exchange
        exchange_pairs = exchange.pairs
        
        for pair in possible_sells:
            exch_pair = exchange_pairs[pair]

            # lowest cost trade-able
            min_cost = exchange.get_min_cost(pair)
            if exch_pair['total'] * exch_pair['close'] < min_cost:
                continue

            orderbook = exchange.get_depth(pair, 'sell')
            if orderbook is None:
                continue

            lowest_sell_price = possible_sells[pair]
            current_price = exch_pair['close']

            can_fill, minimum_fill = process_depth(orderbook, exch_pair['total'], min_cost)
            if can_fill is not None and can_fill.price > lowest_sell_price:
                price = can_fill

            elif minimum_fill is not None and minimum_fill.price > lowest_sell_price:
                price = minimum_fill

            else:
                continue

            current_value = exch_pair['total'] * price.average_price
            
            # profits.append(
            #     (current_value - exch_pair['total_cost']) / exch_pair['total_cost'] * 100)
            order = exchange.place_order(pair, 'limit', 'sell', exch_pair['total'], price.price)
            self.trade_history.append(order)
            self.save_trade_history()

    # ----
    def handle_possible_dca_buys(self, possible_buys):
        # Alleviate lookup cost
        exchange = self.exchange
        config = self.config
        exchange_pairs = exchange.pairs
        
        dca_timeout = config.global_trade_conditions['dca_timeout'] * 60
        for pair in possible_buys:
            exch_pair = exchange_pairs[pair]
            
            # lowest cost trade-able
            min_cost = exchange.get_min_cost(pair)

            if (exch_pair['total'] * exch_pair['close'] < min_cost
                    or time.time() - exch_pair['last_order_time'] < dca_timeout):
                continue

            if self.pair_specific_buy_checks(pair, exch_pair['close'], possible_buys[pair],
                                             exchange.balance, exch_pair['percentage'],
                                             config.global_trade_conditions['dca_min_buy_balance'], True):

                current_price = exch_pair['close']

                # get orderbook, if time since last orderbook check is too soon, it will return none
                orderbook = exchange.get_depth(pair, 'BUY')
                if orderbook is None:
                    continue

                # get viable trade, returns None if none available
                price_info = self.check_for_viable_trade(current_price, orderbook, possible_buys[pair], min_cost,
                                                         config.global_trade_conditions['max_spread'], True)

                # Check to see if amount remaining to buy is greater than min trade quantity for pair
                if price_info is None or price_info.amount * price_info.average_price < min_cost:
                    continue

                order = exchange.place_order(pair, 'limit', 'buy', possible_buys[pair], exch_pair['close'])
                exch_pair['dca_level'] += 1
                self.trade_history.append(order)
                self.save_trade_history()

    # ----
    def pair_specific_buy_checks(self, pair, price, amount, balance, change, min_balance, dca=False):
        # Alleviate lookup cost
        global_trade_conditions = self.config.global_trade_conditions

        min_balance = min_balance if not isinstance(min_balance, str) \
            else percentToFloat(min_balance) * self.get_tcv()

        checks = [not exceeds_min_balance(balance, min_balance, price, amount),
                  below_max_change(change, global_trade_conditions['max_change']),
                  above_min_change(change, global_trade_conditions['min_change']),
                  not is_blacklisted(pair, global_trade_conditions['blacklist']),
                  is_whitelisted(pair, global_trade_conditions['whitelist'])
                  ]

        if not dca:
            checks.append(self.exchange.pairs[pair]['total'] < 0.8 * amount)
            checks.append(below_max_pairs(len(self.owned), global_trade_conditions['max_pairs']))

        return all(checks)

    # ----
    def global_buy_checks(self):
        # Alleviate lookup cost
        quote_change_info = self.exchange.quote_change_info
        market_change = self.config.global_trade_conditions['market_change']

        check_24h_quote_change = in_range(quote_change_info['24h'],
                                          market_change['min_24h_quote_change'],
                                          market_change['max_24h_quote_change'])

        check_1h_quote_change = in_range(quote_change_info['1h'],
                                         market_change['min_1h_quote_change'],
                                         market_change['max_1h_quote_change'])

        check_24h_market_change = in_range(get_average_market_change(self.exchange.pairs),
                                           market_change['min_24h_market_change'],
                                           market_change['max_24h_market_change'])

        return all((
            check_1h_quote_change,
            check_24h_market_change,
            check_24h_quote_change
        ))

    # ----
    def do_technical_analysis(self):
        candles = self.exchange.candles

        for pair in self.exchange.pairs:
            if self.indicators is None:
                raise TypeError('(do_technical_analysis) LiquiTrader.indicators cannot be None')

            try:
                self.statistics[pair] = run_ta(candles[pair], self.indicators)

            except Exception as ex:
                print('err in do ta', pair, ex)
                self.exchange.reload_single_candle_history(pair)
                continue

    # ----
    def save_trade_history(self):
        self.save_pairs_history()
        fp = 'tradehistory.json'
        with open(fp, 'w') as f:
            json.dump(self.trade_history, f)

    # ----
    def save_pairs_history(self):
        fp = 'pair_data.json'
        with open(fp, 'w') as f:
            json.dump(self.exchange.pairs, f)

    # ----
    def load_pairs_history(self):
        fp = 'pair_data.json'

        with open(fp, 'r') as f:
            pair_data = json.load(f)

        exchange_pairs = self.exchange.pairs
        for pair in exchange_pairs:
            if pair in pair_data:
                exch_pair = exchange_pairs[pair]

                if exch_pair['total_cost'] is None:
                    exch_pair.update(pair_data[pair])
                else:
                    exch_pair['dca_level'] = pair_data[pair]['dca_level']
                    exch_pair['last_order_time'] = pair_data[pair]['last_order_time']

    # ----
    def load_trade_history(self):
        fp = 'tradehistory.json'
        with open(fp, 'r') as f:
            self.trade_history = json.load(f)

    # ----
    def pairs_to_df(self, basic=True, friendly=False):
        df = pd.DataFrame.from_dict(self.exchange.pairs, orient='index')

        if 'total_cost' in df:
            df['current_value'] = df.close * df.total
            df['gain'] = (df.close - df.avg_price) / df.avg_price * 100

        if friendly:
            df = df[DEFAULT_COLUMNS] if basic else df
            df.rename(columns=COLUMN_ALIASES,
                      inplace=True)
            return df

        else:
            return df[DEFAULT_COLUMNS] if basic else df

    # ----
    def get_pending_value(self):
        df = self.pairs_to_df()

        if 'total_cost' in df:
            return df.total_cost.sum() + self.exchange.balance
        else:
            return 0

    # ----
    def get_pair(self, symbol):
        return self.exchange.pairs[symbol]

    # ----
    @staticmethod
    def calc_gains_on_df(df):
        df['total_cost'] = df.bought_price * df.filled
        df['gain'] = df['cost'] - df['total_cost']
        df['percent_gain'] = (df['cost'] - df['total_cost']) / df['total_cost'] * 100

        return df

    # ----
    def get_daily_profit_data(self):
        df = pd.DataFrame(self.trade_history + [PaperBinance.create_paper_order(0, 0, 'sell', 0, 0, 0)])
        df = self.calc_gains_on_df(df)

        # todo timezones
        df = df.set_index(
            pd.to_datetime(df.timestamp, unit='ms')
        )

        return df.resample('1d').sum()

    # ----
    def get_pair_profit_data(self):
        df = pd.DataFrame(self.trade_history)
        df = self.calc_gains_on_df(df)

        return df.groupby('symbol').sum()[['total_cost', 'cost', 'amount', 'gain']]

    # ----
    def get_total_profit(self):
        df = pd.DataFrame(self.trade_history)
        df = df[df.side == 'sell']

        # filled is the amount filled
        df['total_cost'] = df.bought_price * df.filled
        df['gain'] = df['cost'] - df['total_cost']

        return df.gain.sum()

    # ----
    def get_cumulative_profit(self):
        return self.get_daily_profit_data().cumsum()
    #     (current_value - self.exchange.pairs[pair]['total_cost']) / self.exchange.pairs[pair]['total_cost'] * 100)


# ----
def main():
    def err_msg():
        sys.stdout.write('LiquiTrader has been illegitimately modified and must be reinstalled.\n')
        sys.stdout.write('We recommend downloading it manually from our website in case your updater has been compromised.\n\n')
        sys.stdout.flush()

    print('Starting LiquiTrader...\n')

    if hasattr(sys, 'frozen') or not os.path.isfile('.gitignore'):
        vfile = 'lib/verifier.cp36-win_amd64.pyd' if sys.platform == 'win32' else 'lib/verifier.cpython-36m-x86_64-linux-gnu.so'

        # Check that verifier exists and that it is of a reasonable size
        if (not os.path.isfile(vfile)) or os.stat(vfile).st_size < 260000:
            err_msg()
            sys.exit(1)

        start = time.time()
        verifier.verify()

        # Check that verifier took a reasonable amount of time to execute (make NOPing harder)
        if (time.time() - start) < .05:
            err_msg()
            sys.exit(1)

    from webserver import webserver

    shutdown_in_progress_event = threading.Event()
    shutdown_complete_event = threading.Event()

    global LT_TRADER
    LT_TRADER = LiquiTrader()
    LT_TRADER.initialize_config()

    webserver.LT_TRADER = LT_TRADER

    try:
        LT_TRADER.load_trade_history()
    except FileNotFoundError:
        print('No trade history found')

    LT_TRADER.initialize_exchange()

    try:
        LT_TRADER.load_pairs_history()
    except FileNotFoundError:
        print('No pairs history found')

    LT_TRADER.load_strategies()

    def run(_shutdown_event, _shutdown_complete_event):
        # Alleviate method lookup overhead
        global_buy_checks = LT_TRADER.global_buy_checks
        do_technical_analysis = LT_TRADER.do_technical_analysis
        get_possible_buys = LT_TRADER.get_possible_buys
        handle_possible_buys = LT_TRADER.handle_possible_buys
        handle_possible_dca_buys = LT_TRADER.handle_possible_dca_buys
        get_possible_sells = LT_TRADER.get_possible_sells
        handle_possible_sells = LT_TRADER.handle_possible_sells

        exchange = LT_TRADER.exchange

        while not _shutdown_event.is_set():
            try:
                # timed @ 1.1 seconds 128ms stdev
                do_technical_analysis()

                if global_buy_checks():
                    possible_buys = get_possible_buys(exchange.pairs, LT_TRADER.buy_strategies)
                    handle_possible_buys(possible_buys)
                    possible_dca_buys = get_possible_buys(exchange.pairs, LT_TRADER.dca_buy_strategies)
                    handle_possible_dca_buys(possible_dca_buys)

                possible_sells = get_possible_sells(exchange.pairs, LT_TRADER.sell_strategies)
                handle_possible_sells(possible_sells)

            except Exception as ex:
                print('err in run: {}'.format(traceback.format_exc()))

    lt_thread = threading.Thread(target=lambda: run(shutdown_in_progress_event,
                                                    shutdown_complete_event))
    gui_thread = threading.Thread(target=lambda: webserver.app.run('0.0.0.0', 80))
    exchange_thread = threading.Thread(target=lambda: LT_TRADER.exchange.start(shutdown_in_progress_event,
                                                                               shutdown_complete_event))

    lt_thread.start()
    gui_thread.start()
    exchange_thread.start()

    while True:
        try:
            input()

        except KeyboardInterrupt:
            print('\nClosing LiquiTrader...\n')

            shutdown_in_progress_event.set()  # Set shutdown flag
            webserver.app.stop()  # Gracefully shut down webserver

            # Wait for transactions / critical actions to finish
            if not shutdown_complete_event.is_set():
                print('Waiting for transactions to complete...')

                while not shutdown_complete_event.is_set():
                    time.sleep(.5)

            print('\nThanks for using LiquiTrader!\n')
            return


if __name__ == '__main__':
    def get_pc():
        df = LT_TRADER.pairs_to_df()
        df[df['total'] > 0]
        return df

    main()

    # df['% Change'].dropna()
    # Out[8]:
    # CLOAK / ETH - 104.763070
    # DGD / ETH
    # 0.329280
    # EDO / ETH - 0.100000
    # FUN / ETH - 0.691351
    # GTO / ETH - 2.135282
    # ICX / ETH - 0.563066
    # IOTA / ETH - 0.338263
    # REQ / ETH
    # 0.049041
    # SNM / ETH
    # 0.588560
    # STORJ / ETH - 0.997334
    # TRX / ETH - 2.530675
    # Name: % Change, dtype: float64
