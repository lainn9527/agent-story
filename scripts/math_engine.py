"""
Mathematical Expression Evaluator using Reverse Polish Notation (RPN).

Inspired by RisuAI's math calculation system for damage formulas and stat calculations.
"""

import re
import math
from typing import Dict, Any, Union, List
from functools import lru_cache


class MathEngine:
    """
    Evaluates mathematical expressions with variable substitution.

    Supports:
    - Basic operators: +, -, *, /, ^ (power), % (modulo)
    - Logical operators: &, |, !, >, <, >=, <=, =, !=
    - Variables: $variable_name or @global_variable
    - Functions: abs, ceil, floor, round, min, max, sqrt
    - Parentheses for grouping
    """

    def __init__(self, precision: int = 2):
        """
        Initialize the math engine.

        Args:
            precision: Number of decimal places for results
        """
        self.precision = precision
        self.operators = {
            '+': (2, 'L'),
            '-': (2, 'L'),
            '*': (3, 'L'),
            '/': (3, 'L'),
            '%': (3, 'L'),
            '^': (4, 'R'),
            '&': (1, 'L'),  # Logical AND
            '|': (1, 'L'),  # Logical OR
            '!': (5, 'R'),  # Logical NOT (unary)
            '>': (1, 'L'),
            '<': (1, 'L'),
            '>=': (1, 'L'),
            '<=': (1, 'L'),
            '=': (1, 'L'),
            '!=': (1, 'L'),
        }

    def _tokenize(self, expression: str) -> List[str]:
        """
        Tokenize an expression into components.

        Args:
            expression: Math expression string

        Returns:
            List of tokens
        """
        # Replace comparison operators
        expression = expression.replace('>=', ' >= ')
        expression = expression.replace('<=', ' <= ')
        expression = expression.replace('!=', ' != ')
        expression = expression.replace('==', ' = ')
        expression = expression.replace('>', ' > ')
        expression = expression.replace('<', ' < ')
        expression = expression.replace('=', ' = ')

        # Tokenize using regex
        pattern = r'(\d+\.?\d*|[+\-*/%^&|!()]|\$\w+|@\w+|\w+)'
        tokens = re.findall(pattern, expression)

        return [t.strip() for t in tokens if t.strip()]

    def _to_rpn(self, tokens: List[str]) -> List[str]:
        """
        Convert infix notation to Reverse Polish Notation.

        Uses the Shunting Yard algorithm.

        Args:
            tokens: List of expression tokens

        Returns:
            List of tokens in RPN order
        """
        output = []
        operator_stack = []

        for token in tokens:
            if self._is_number(token) or token.startswith('$') or token.startswith('@'):
                # Numbers and variables go directly to output
                output.append(token)

            elif token in ('abs', 'ceil', 'floor', 'round', 'min', 'max', 'sqrt'):
                # Functions go to operator stack
                operator_stack.append(token)

            elif token in self.operators:
                # Handle operators with precedence
                precedence, associativity = self.operators[token]

                while operator_stack:
                    top = operator_stack[-1]
                    if top == '(':
                        break

                    if top in self.operators:
                        top_prec, _ = self.operators[top]
                        if (associativity == 'L' and precedence <= top_prec) or \
                           (associativity == 'R' and precedence < top_prec):
                            output.append(operator_stack.pop())
                        else:
                            break
                    elif top in ('abs', 'ceil', 'floor', 'round', 'min', 'max', 'sqrt'):
                        output.append(operator_stack.pop())
                    else:
                        break

                operator_stack.append(token)

            elif token == '(':
                operator_stack.append(token)

            elif token == ')':
                # Pop until matching '('
                while operator_stack and operator_stack[-1] != '(':
                    output.append(operator_stack.pop())

                if operator_stack and operator_stack[-1] == '(':
                    operator_stack.pop()  # Remove '('

                # If there's a function on top, pop it too
                if operator_stack and operator_stack[-1] in ('abs', 'ceil', 'floor', 'round', 'min', 'max', 'sqrt'):
                    output.append(operator_stack.pop())

        # Pop remaining operators
        while operator_stack:
            output.append(operator_stack.pop())

        return output

    def _is_number(self, token: str) -> bool:
        """Check if a token is a number."""
        try:
            float(token)
            return True
        except ValueError:
            return False

    def _evaluate_rpn(self, rpn_tokens: List[str], variables: Dict[str, Any]) -> float:
        """
        Evaluate an RPN expression.

        Args:
            rpn_tokens: Tokens in RPN order
            variables: Variable name -> value mapping

        Returns:
            Evaluation result
        """
        stack = []

        for token in rpn_tokens:
            if self._is_number(token):
                stack.append(float(token))

            elif token.startswith('$') or token.startswith('@'):
                # Variable substitution
                var_name = token[1:]  # Remove $ or @
                value = variables.get(var_name, 0)
                stack.append(float(value))

            elif token in ('abs', 'ceil', 'floor', 'round', 'sqrt'):
                # Unary functions
                if len(stack) < 1:
                    raise ValueError(f"Not enough operands for {token}")

                a = stack.pop()

                if token == 'abs':
                    result = abs(a)
                elif token == 'ceil':
                    result = math.ceil(a)
                elif token == 'floor':
                    result = math.floor(a)
                elif token == 'round':
                    result = round(a)
                elif token == 'sqrt':
                    result = math.sqrt(a)

                stack.append(result)

            elif token in ('min', 'max'):
                # Binary functions
                if len(stack) < 2:
                    raise ValueError(f"Not enough operands for {token}")

                b = stack.pop()
                a = stack.pop()

                if token == 'min':
                    result = min(a, b)
                elif token == 'max':
                    result = max(a, b)

                stack.append(result)

            elif token in self.operators:
                if token == '!':
                    # Unary NOT
                    if len(stack) < 1:
                        raise ValueError("Not enough operands for !")
                    a = stack.pop()
                    result = 1 if not a else 0
                    stack.append(result)
                else:
                    # Binary operators
                    if len(stack) < 2:
                        raise ValueError(f"Not enough operands for {token}")

                    b = stack.pop()
                    a = stack.pop()

                    if token == '+':
                        result = a + b
                    elif token == '-':
                        result = a - b
                    elif token == '*':
                        result = a * b
                    elif token == '/':
                        if b == 0:
                            raise ValueError("Division by zero")
                        result = a / b
                    elif token == '%':
                        result = a % b
                    elif token == '^':
                        result = a ** b
                    elif token == '&':
                        result = 1 if (a and b) else 0
                    elif token == '|':
                        result = 1 if (a or b) else 0
                    elif token == '>':
                        result = 1 if a > b else 0
                    elif token == '<':
                        result = 1 if a < b else 0
                    elif token == '>=':
                        result = 1 if a >= b else 0
                    elif token == '<=':
                        result = 1 if a <= b else 0
                    elif token == '=':
                        result = 1 if a == b else 0
                    elif token == '!=':
                        result = 1 if a != b else 0

                    stack.append(result)

        if len(stack) != 1:
            raise ValueError("Invalid expression")

        return stack[0]

    def evaluate(self, expression: str, variables: Dict[str, Any] = None) -> Union[float, str]:
        """
        Evaluate a mathematical expression.

        Args:
            expression: Math expression string (e.g., "(5 + $strength) * 1.5 - 2")
            variables: Variable name -> value mapping

        Returns:
            Evaluation result, rounded to specified precision
        """
        if variables is None:
            variables = {}

        try:
            # Tokenize
            tokens = self._tokenize(expression)

            # Convert to RPN
            rpn = self._to_rpn(tokens)

            # Evaluate
            result = self._evaluate_rpn(rpn, variables)

            # Round to precision
            if self.precision == 0:
                return int(result)
            else:
                return round(result, self.precision)

        except Exception as e:
            return f"[計算錯誤: {str(e)}]"

    def process_text(self, text: str, variables: Dict[str, Any] = None) -> str:
        """
        Process text and evaluate all <!--CALC expr CALC--> tags.

        Args:
            text: Text containing CALC tags
            variables: Variable name -> value mapping

        Returns:
            Text with CALC tags replaced by their results
        """
        if variables is None:
            variables = {}

        # Pattern to match <!--CALC expr CALC-->
        pattern = r'<!--CALC\s+(.+?)\s+CALC-->'

        def replace_calc(match):
            expression = match.group(1)
            result = self.evaluate(expression, variables)
            return str(result)

        return re.sub(pattern, replace_calc, text, flags=re.IGNORECASE | re.DOTALL)


def create_math_engine(config: Dict[str, Any]) -> MathEngine:
    """
    Create a MathEngine from configuration.

    Args:
        config: Feature configuration dict with optional 'precision' key

    Returns:
        Configured MathEngine instance
    """
    precision = config.get("precision", 2)
    return MathEngine(precision=precision)
