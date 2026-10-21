"""Recognize narrowly defined provider outcomes without claiming interval coverage."""
import re


def no_results_error(error):
    value = str(error or '').strip()
    return value in {'Posts for the specified period were not found', 'Activities are not found'} or bool(
        re.fullmatch(r'Total posts on page: \d+\. Posts with parseable dates: \d+\. '
                     r'Posts matching requested period \([^\r\n]*\): 0\.'
                     r'(?: Note: not all posts contain dates,[^\r\n]*)?', value))


def retryable_read_error(error):
    return no_results_error(error) or str(error or '').strip() == 'Crawler error: Minimal layout detected, retrying'
