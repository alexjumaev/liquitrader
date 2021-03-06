from conditions.Condition import Condition
from conditions.condition_tools import evaluate_condition
from utils.Utils import get_current_value, get_percent_change

import time

class SellCondition(Condition):

    def __init__(self, condition_config: dict, pair_settings=None):
        super().__init__(condition_config, pair_settings)
        self.sell_value = float(condition_config['sell_value'])

    def get_lowest_sell_price(self, bought_price, fee, sell_value=None):
        if sell_value is None:
            return bought_price * ((1 + self.sell_value + fee) / 100)
        else:
            return bought_price * ((1 + float(sell_value) + fee) / 100)

    def get_sell_value(self, pair):
        pair_settings = self.pair_settings
        id = pair.split("/")[0]
        if pair_settings is None or id not in pair_settings or "sell" not in pair_settings[id]:
            return self.sell_value
        else:
            return float(pair_settings[id]["sell"]["value"])


    def evaluate(self, pair: dict, indicators: dict, balance: float=None, fee=0.075):
        """
        evaluate single pair against conditions
        if not in pairs_trailing and conditions = true : add to dict, set floor/ceiling at price -> return true
        else if conditions = false : remove from pairs_trailing -> return False
        else if in pairs_trailing and conditions = true and trail > trailing value: return amount to buy/sell
        :param pair:
        :return:
        """
        symbol = pair['symbol']
        sell_value = self.get_sell_value(symbol)

        if 'total' not in pair or 'bid' not in pair or 'total_cost' not in pair or 'avg_price' not in pair or pair['avg_price'] is None:
            return None

        price = float(pair['bid'])
        trail_to = None
        current_value = pair['total'] * price
        total_cost = pair['avg_price'] * pair['total']
        if total_cost is None:
            return None
        percent_change = get_percent_change(current_value, total_cost) - fee
        pair['percent_change'] = percent_change
        analysis = [evaluate_condition(condition, pair, indicators, is_buy=False) for condition in self.conditions_list]

        # check percent change, if above trigger return none
        res = False not in analysis and percent_change > sell_value if sell_value >= 0 else percent_change < sell_value

        if res and symbol in self.pairs_trailing:
            current_marker = self.pairs_trailing[symbol]['trail_from']
            marker = price if price > current_marker else current_marker
            trail_to = marker * (1 - (self.trailing_value/100))
            self.pairs_trailing[symbol] = self.trail_to(marker, trail_to, pair, indicators)

        elif res:
            trail_to = price * (1 - (self.trailing_value / 100))
            self.pairs_trailing[symbol] = self.trail_to(price, trail_to, pair, indicators)

        elif not res:
            if symbol in self.pairs_trailing: self.pairs_trailing.pop(symbol)
            return None

        if price <= trail_to and not trail_to is None:
            return self.get_lowest_sell_price(pair['avg_price'], fee)


if __name__ == '__main__':
    from conditions.examples import *
    strategy = {}
    # 1 and 3 are true with the test data
    strategy['conditions'] = [condition_1]
    strategy['trailing %'] = 0.1
    strategy['sell_value'] = 1
    pair1['total_cost'] = 1
    pair1['total'] = 1
    cond = SellCondition(strategy)
    print(vars(cond))
    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)
    pair1['close'] = 1

    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)

    pair1['close'] = 1.3

    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)

    pair1['close'] = 1.1

    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)