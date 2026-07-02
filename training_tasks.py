"""training_tasks — curated pool of grounded practice tasks for `/train`.

Each task is a small, self-contained coding prompt with an assert-based check
so a model's solution can be executed and graded automatically (see
grounding.run_code). Stdlib only.
"""
import random

TASKS = [
    {"name": "reverse_string",
     "prompt": "Write a Python function named `reverse_string(s)` that returns the string reversed. Return ONLY the function in one python code block.",
     "check": "assert reverse_string('hello') == 'olleh'\nassert reverse_string('') == ''"},

    {"name": "factorial",
     "prompt": "Write a Python function named `factorial(n)` that returns n! (0! == 1) using iteration or recursion. Return ONLY the function in one python code block.",
     "check": "assert factorial(0) == 1\nassert factorial(5) == 120"},

    {"name": "is_prime",
     "prompt": "Write a Python function named `is_prime(n)` that returns True if n is a prime number, False otherwise (n may be < 2). Return ONLY the function in one python code block.",
     "check": "assert is_prime(2) is True\nassert is_prime(1) is False\nassert is_prime(17) is True\nassert is_prime(18) is False"},

    {"name": "fizzbuzz",
     "prompt": "Write a Python function named `fizzbuzz(n)` that returns a list of strings for 1..n where multiples of 3 are 'Fizz', multiples of 5 are 'Buzz', multiples of both are 'FizzBuzz', and everything else is str(number). Return ONLY the function in one python code block.",
     "check": "assert fizzbuzz(5) == ['1','2','Fizz','4','Buzz']\nassert fizzbuzz(15)[-1] == 'FizzBuzz'"},

    {"name": "count_vowels",
     "prompt": "Write a Python function named `count_vowels(s)` that returns the number of vowels (a, e, i, o, u, case-insensitive) in s. Return ONLY the function in one python code block.",
     "check": "assert count_vowels('Hello World') == 3\nassert count_vowels('') == 0"},

    {"name": "nth_fibonacci",
     "prompt": "Write a Python function named `nth_fibonacci(n)` that returns the nth Fibonacci number (0-indexed, fib(0)==0, fib(1)==1). Return ONLY the function in one python code block.",
     "check": "assert nth_fibonacci(0) == 0\nassert nth_fibonacci(1) == 1\nassert nth_fibonacci(10) == 55"},

    {"name": "sum_even",
     "prompt": "Write a Python function named `sum_even(nums)` that returns the sum of the even numbers in a list. Return ONLY the function in one python code block.",
     "check": "assert sum_even([1,2,3,4,5,6]) == 12\nassert sum_even([]) == 0"},

    {"name": "celsius_to_fahrenheit",
     "prompt": "Write a Python function named `celsius_to_fahrenheit(c)` that converts Celsius to Fahrenheit. Return ONLY the function in one python code block.",
     "check": "assert celsius_to_fahrenheit(0) == 32\nassert abs(celsius_to_fahrenheit(100) - 212) < 1e-9"},

    {"name": "flatten",
     "prompt": "Write a Python function named `flatten(nested)` that flattens a list of lists (one level deep) into a single list. Return ONLY the function in one python code block.",
     "check": "assert flatten([[1,2],[3],[4,5,6]]) == [1,2,3,4,5,6]\nassert flatten([]) == []"},

    {"name": "word_count",
     "prompt": "Write a Python function named `word_count(s)` that returns a dict mapping each word (whitespace-split) to its number of occurrences. Return ONLY the function in one python code block.",
     "check": "assert word_count('a b a c b a') == {'a':3,'b':2,'c':1}"},

    {"name": "is_palindrome",
     "prompt": "Write a Python function named `is_palindrome(s)` that returns True if s reads the same forwards and backwards, False otherwise (exact characters, no normalization). Return ONLY the function in one python code block.",
     "check": "assert is_palindrome('racecar') is True\nassert is_palindrome('hello') is False"},

    {"name": "merge_sorted",
     "prompt": "Write a Python function named `merge_sorted(a, b)` that merges two already-sorted lists into one sorted list. Return ONLY the function in one python code block.",
     "check": "assert merge_sorted([1,3,5],[2,4,6]) == [1,2,3,4,5,6]\nassert merge_sorted([],[1,2]) == [1,2]"},

    {"name": "gcd",
     "prompt": "Write a Python function named `gcd(a, b)` that returns the greatest common divisor of two non-negative integers. Return ONLY the function in one python code block.",
     "check": "assert gcd(12, 18) == 6\nassert gcd(7, 0) == 7"},

    {"name": "title_case",
     "prompt": "Write a Python function named `title_case(s)` that returns s with the first letter of each word capitalized and the rest lowercase. Return ONLY the function in one python code block.",
     "check": "assert title_case('hello world') == 'Hello World'\nassert title_case('THE CAT sat') == 'The Cat Sat'"},

    {"name": "dedupe",
     "prompt": "Write a Python function named `dedupe(items)` that returns a list with duplicates removed, preserving the first occurrence's order. Return ONLY the function in one python code block.",
     "check": "assert dedupe([1,2,1,3,2,4]) == [1,2,3,4]\nassert dedupe([]) == []"},

    {"name": "roman_to_int",
     "prompt": "Write a Python function named `roman_to_int(s)` that converts a Roman numeral string to an integer. Return ONLY the function in one python code block.",
     "check": "assert roman_to_int('III') == 3\nassert roman_to_int('IX') == 9\nassert roman_to_int('LVIII') == 58\nassert roman_to_int('MCMXCIV') == 1994"},

    {"name": "count_matches",
     "prompt": "Write a Python function named `count_matches(items, predicate)` that returns how many elements of items satisfy predicate(item) == True. Return ONLY the function in one python code block.",
     "check": "assert count_matches([1,2,3,4,5], lambda x: x % 2 == 0) == 2\nassert count_matches([], lambda x: True) == 0"},

    {"name": "to_jsonl",
     "prompt": "Write a Python function named `to_jsonl(records)` that takes a list of dicts and returns a single string with one JSON object per line (use the json module). Return ONLY the function in one python code block.",
     "check": "import json\nout = to_jsonl([{'a':1},{'b':2}])\nlines = out.strip().split('\\n')\nassert json.loads(lines[0]) == {'a':1}\nassert json.loads(lines[1]) == {'b':2}"},

    {"name": "parse_kv",
     "prompt": "Write a Python function named `parse_kv(s)` that parses a string like 'a=1,b=2,c=3' into a dict {'a':'1','b':'2','c':'3'} (string values). Return ONLY the function in one python code block.",
     "check": "assert parse_kv('a=1,b=2,c=3') == {'a':'1','b':'2','c':'3'}\nassert parse_kv('') == {}"},

    {"name": "group_counts",
     "prompt": "Write a Python function named `group_counts(items)` that returns a dict mapping each distinct item to how many times it appears in the list. Return ONLY the function in one python code block.",
     "check": "assert group_counts(['a','b','a','c','b','a']) == {'a':3,'b':2,'c':1}"},

    {"name": "top_n",
     "prompt": "Write a Python function named `top_n(nums, n)` that returns the n largest values from nums, sorted descending. Return ONLY the function in one python code block.",
     "check": "assert top_n([5,1,9,3,7], 3) == [9,7,5]\nassert top_n([1], 5) == [1]"},

    {"name": "extract_numbers",
     "prompt": "Write a Python function named `extract_numbers(s)` that returns a list of all integers found in a string, as ints. Return ONLY the function in one python code block.",
     "check": "assert extract_numbers('a1 b22 c-3 d') == [1, 22, -3]\nassert extract_numbers('no numbers') == []"},

    {"name": "chunk",
     "prompt": "Write a Python function named `chunk(items, size)` that splits items into a list of lists, each of at most `size` elements (last chunk may be shorter). Return ONLY the function in one python code block.",
     "check": "assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\nassert chunk([], 3) == []"},

    {"name": "merge_dicts",
     "prompt": "Write a Python function named `merge_dicts(a, b)` that returns a new dict with keys/values from both a and b, where b's values win on key conflicts. Return ONLY the function in one python code block.",
     "check": "assert merge_dicts({'x':1,'y':2}, {'y':3,'z':4}) == {'x':1,'y':3,'z':4}"},

    {"name": "safe_int",
     "prompt": "Write a Python function named `safe_int(s, default=0)` that tries to convert s to an int, returning default if conversion fails. Return ONLY the function in one python code block.",
     "check": "assert safe_int('42') == 42\nassert safe_int('nope', -1) == -1\nassert safe_int('nope') == 0"},

    {"name": "is_anagram",
     "prompt": "Write a Python function named `is_anagram(a, b)` that returns True if strings a and b are anagrams of each other (same letters, ignoring case, same multiset), False otherwise. Return ONLY the function in one python code block.",
     "check": "assert is_anagram('listen', 'silent') is True\nassert is_anagram('Hello', 'World') is False"},

    {"name": "run_length_encode",
     "prompt": "Write a Python function named `run_length_encode(s)` that run-length encodes a string, e.g. 'aaabbc' -> 'a3b2c1'. Return ONLY the function in one python code block.",
     "check": "assert run_length_encode('aaabbc') == 'a3b2c1'\nassert run_length_encode('') == ''"},

    {"name": "binary_search",
     "prompt": "Write a Python function named `binary_search(sorted_list, target)` that returns the index of target in sorted_list using binary search, or -1 if not found. Return ONLY the function in one python code block.",
     "check": "assert binary_search([1,3,5,7,9,11], 7) == 3\nassert binary_search([1,3,5,7,9,11], 4) == -1"},

    {"name": "is_balanced",
     "prompt": "Write a Python function named `is_balanced(s)` that returns True if all brackets ()[]{} in s are balanced and properly nested, False otherwise. Return ONLY the function in one python code block.",
     "check": "assert is_balanced('([]{})') is True\nassert is_balanced('([)]') is False\nassert is_balanced('') is True"},

    {"name": "to_snake_case",
     "prompt": "Write a Python function named `to_snake_case(s)` that converts a camelCase or PascalCase string to snake_case, e.g. 'HelloWorld' -> 'hello_world'. Return ONLY the function in one python code block.",
     "check": "assert to_snake_case('HelloWorld') == 'hello_world'\nassert to_snake_case('camelCase') == 'camel_case'"},

    {"name": "second_largest",
     "prompt": "Write a Python function named `second_largest(nums)` that returns the second largest distinct value in a list of numbers. Return ONLY the function in one python code block.",
     "check": "assert second_largest([5,1,9,9,3]) == 5\nassert second_largest([1,2]) == 1"},

    {"name": "most_frequent",
     "prompt": "Write a Python function named `most_frequent(items)` that returns the most frequently occurring element in a non-empty list (any one of the tied winners is acceptable). Return ONLY the function in one python code block.",
     "check": "assert most_frequent([1,2,2,3,2]) == 2\nassert most_frequent(['a']) == 'a'"},

    {"name": "caesar_cipher",
     "prompt": "Write a Python function named `caesar_cipher(s, shift)` that shifts each letter in s by `shift` positions in the alphabet, preserving case and leaving non-letters unchanged. Return ONLY the function in one python code block.",
     "check": "assert caesar_cipher('abc', 1) == 'bcd'\nassert caesar_cipher('xyz', 3) == 'abc'\nassert caesar_cipher('Hi!', 1) == 'Ij!'"},

    {"name": "transpose",
     "prompt": "Write a Python function named `transpose(matrix)` that returns the transpose of a 2D list (list of lists) of numbers. Return ONLY the function in one python code block.",
     "check": "assert transpose([[1,2,3],[4,5,6]]) == [[1,4],[2,5],[3,6]]"},

    {"name": "digit_sum",
     "prompt": "Write a Python function named `digit_sum(n)` that returns the sum of the digits of a non-negative integer n. Return ONLY the function in one python code block.",
     "check": "assert digit_sum(1234) == 10\nassert digit_sum(0) == 0"},

    {"name": "is_leap_year",
     "prompt": "Write a Python function named `is_leap_year(year)` that returns True if year is a leap year per the Gregorian calendar rules, False otherwise. Return ONLY the function in one python code block.",
     "check": "assert is_leap_year(2000) is True\nassert is_leap_year(1900) is False\nassert is_leap_year(2024) is True\nassert is_leap_year(2023) is False"},

    {"name": "clamp",
     "prompt": "Write a Python function named `clamp(value, lo, hi)` that returns value clamped to the inclusive range [lo, hi]. Return ONLY the function in one python code block.",
     "check": "assert clamp(5, 0, 10) == 5\nassert clamp(-1, 0, 10) == 0\nassert clamp(99, 0, 10) == 10"},

    {"name": "rotate_list",
     "prompt": "Write a Python function named `rotate_list(items, k)` that rotates the list left by k positions (k may be larger than len(items)). Return ONLY the function in one python code block.",
     "check": "assert rotate_list([1,2,3,4,5], 2) == [3,4,5,1,2]\nassert rotate_list([1,2,3], 0) == [1,2,3]"},

    {"name": "longest_word",
     "prompt": "Write a Python function named `longest_word(s)` that returns the longest whitespace-separated word in s (first one wins ties). Return ONLY the function in one python code block.",
     "check": "assert longest_word('the quick brown fox') == 'quick'\nassert longest_word('a bb') == 'bb'"},
]


def sample(n):
    """Return up to n distinct random tasks from TASKS."""
    return random.sample(TASKS, min(n, len(TASKS)))
