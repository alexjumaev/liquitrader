from BuyCondition import *
from utils.Utils import get_current_value, get_percent_change

class DCABuyCondition(Condition):
    """
    Preform all the same functionality as buy_condition but also handle "DCA strategy"
    DCA strategy will be in the form of a dict
    Minimum required for DCA strategy example
    {"default": {"trigger": -3, "percentage": 100}}
    additional level options:
    {"default": {"trigger": -3, "percentage": 100},
                "0": {"trigger": -5, "percentage": 50},
                "3":{"trigger": -2, "percentage": 25}
                }
    In this example: pair @ dca level 0, triggers at -5%, for 50% of value
    then level 1 and 2 trigger at -3% for 100%
    level 3 at -2% for 25%
    level 4+ same as 1 and 2
    """

    def __init__(self, condition_config: dict):
        super().__init__(condition_config)
        self.dca_strategy = condition_config['dca_strategy']
        self.max_dca_level = condition_config['max_dca_level']

    def evaluate(self, pair: dict, indicators: dict):
        """
        evaluate single pair against conditions
        if not in pairs_trailing and conditions = true : add to dict, set floor/ceiling at price -> return true
        else if conditions = false : remove from pairs_trailing -> return False
        else if in pairs_trailing and conditions = true and trail > trailing value: return amount to buy/sell
        :param pair:
        :return:
        """
        if 'total' not in pair or 'close' not in pair:
            return None
        if 'dca_level' not in pair:
            pair['dca_level'] = 1
        trail_to = None
        symbol = pair['symbol']
        price = pair['close']
        dca_level = pair['dca_level']

        # check to make sure we're below max dca level (# of times DCA'd)
        if dca_level >= self.max_dca_level:
            print('cannot dca {}: max dca level'.format(symbol))
            return None

        # get current value, then use to calc percent change
        current_value = get_current_value(price, pair['total'])
        percent_change = get_percent_change(current_value, pair['total_cost'])

        # check percent change, if above trigger return none
        if percent_change > self.get_dca_trigger(dca_level):
            print('{}: above dca trigger {}:, currently at {}'.format(symbol, self.get_dca_trigger(dca_level),percent_change))
            return None

        # evaluate all conditions return list of bools
        analysis = [evaluate_condition(condition, pair, indicators) for condition in self.conditions_list]

        # if any are false, result is false
        res = False not in analysis

        # if we're already trailing, update trail_to if needed
        if res and symbol in self.pairs_trailing:
            current_marker = self.pairs_trailing[symbol]['trail_from']
            marker = price if price < current_marker else current_marker
            trail_to = marker * (1 + (self.trailing_value/100))
            self.pairs_trailing[symbol] = {'trail_from':marker, 'trail_to':trail_to }

        # if its not trailing, add to trailing pairs, set trail_to
        elif res:
            trail_to = price * (1 + (self.trailing_value / 100))
            self.pairs_trailing[symbol] = {'trail_from': price, 'trail_to':trail_to}

        # coniditions are false, remove pair from trailing if needed
        elif not res:
            if symbol in self.pairs_trailing: self.pairs_trailing.pop(symbol)
            return None
        # finally if we're above trail_to, return amount to buy
        if price >= trail_to and not trail_to is None:
            return self.get_dca_percent(dca_level)/100 *pair['total']

    # get DCA trigger for the level, if not specified return default
    def get_dca_trigger(self, level):
        if str(level) in self.dca_strategy:
            return self.dca_strategy['default']['trigger'] if 'trigger' not in self.dca_strategy[str(level)]\
                else self.dca_strategy[str(level)]['trigger']
        else:
            return self.dca_strategy['default']['trigger']

    # get DCA percentage for the level, if not specified return default
    def get_dca_percent(self, level):
        if str(level) in self.dca_strategy:
            return self.dca_strategy['default']['percentage'] if 'percentage' not in self.dca_strategy[str(level)]\
                else self.dca_strategy[str(level)]['percentage']
        else:
            return self.dca_strategy['default']['percentage']


if __name__ == '__main__':
    from testconditions import *
    strategy = {}
    # 1 and 3 are true with the test data
    strategy['conditions'] = [condition_1, condition_3]
    strategy['trailing %'] = 0.1
    strategy['max_dca_level'] = 5
    strategy['dca_strategy'] = {"default": {"trigger": -3, "percentage": 100},
                                "0": {"trigger": -5, "percentage": 50},
                                "3":{"trigger": -2, "percentage": 25}
                                }
    cond = DCABuyCondition(strategy)
    pair1['dca_level'] = 1
    pair1['total_cost'] = 3
    pair1['total'] = 1
    print(vars(cond))
    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)
    pair1['close'] = 4.005

    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)

    pair1['close'] = 2

    print(cond.evaluate(pair1,indicators1))
    print(cond.pairs_trailing)

    pair1['close'] = 2.3
    print(cond.pairs_trailing)
    print(cond.evaluate(pair1,indicators1))
