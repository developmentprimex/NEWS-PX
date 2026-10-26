from types import SimpleNamespace

import pytest

from app.intelligence.ai_service import fallback_analysis
from app.intelligence.services import CollectedNewsItem


def analyze(title, body):
    return fallback_analysis(CollectedNewsItem(None, 'https://example.org/post', title, body, None),
                             SimpleNamespace(priority='NORMAL', categories=[], type='LINKEDIN'))


@pytest.mark.parametrize('title,body', [
    ('#horizoneurope #cluster4 | Prishtina REA',
     'JUST OPENED! NEW HORIZON EUROPE FUNDING OPPORTUNITIES! '
     'New Approaches for Human/AI Collaboration. Deadline: 2 February 2027.'),
    ('📢 𝗩𝗔𝗟𝗟𝗘𝗬𝗦 𝗢𝗣𝗘𝗡 𝗖𝗔𝗟𝗟 – 𝗖𝗔𝗦𝗖𝗔𝗗𝗘 𝗙𝗨𝗡𝗗𝗜𝗡𝗚!',
     'The VALLEYS Project invites intermediaries to apply for funding.'),
    ('#valleysproject #opencall | Prishtina REA',
     'Funding for intermediaries: apply for funding to deploy research results in practical pilots.'),
])
def test_social_calls_are_classified_from_normalized_title_and_body(title, body):
    assert analyze(title, body).category == 'GRANT'


@pytest.mark.parametrize('title,body', [
    ('CALL FOR BUSINESSES IN KOSOVO', 'The Commission is collecting business feedback. Share your experience.'),
    ('#fundingnews', 'The government discussed funding opportunities and economic policy.'),
    ('#policy', 'A new report discusses grants and procurement policy.'),
    ('Grant award results', 'The winners have been selected for the grant call.'),
    ('#community', 'Winners of the grant competition are announced. Applications closed.'),
    ('LISTA E PËRFITUESVE', 'Thirrje për aplikim për grantet e publikuara më herët.'),
])
def test_feedback_policy_and_results_are_not_grants(title, body):
    assert analyze(title, body).category == 'NEWS'


def test_verified_topic_tags_do_not_infer_country_eligibility_or_dates():
    result = analyze('#horizoneurope', 'Open funding opportunities for AI and software. Apply now.')
    assert result.category == 'GRANT'
    assert set(result.tags) == {'AI', 'Technology'}
    assert result.eligibility is None and result.deadline is None and result.fundingAmount is None


def test_tender_in_post_body_is_detected():
    assert analyze('#prishtinarea', 'Open procurement tender for software services.').category == 'TENDER'

def test_website_related_links_do_not_turn_policy_pages_into_grants():
    post=CollectedNewsItem(None,'https://example.org/terms','Terms of use','Related news: grant call, apply now.',None)
    assert fallback_analysis(post,SimpleNamespace(priority='NORMAL',categories=[],type='WEBSITE')).category=='NEWS'

def test_dedicated_official_funding_entry_is_a_grant_even_without_call_in_title():
    post=CollectedNewsItem(None,'https://digital-strategy.ec.europa.eu/en/funding/new-human-ai-collaboration','New approaches for Human/AI collaboration','A funding opportunity.',None)
    assert fallback_analysis(post,SimpleNamespace(priority='NORMAL',categories=[],type='WEBSITE')).category=='GRANT'

def test_official_funding_section_retains_tender_category():
    post=CollectedNewsItem(None,'https://digital-strategy.ec.europa.eu/en/funding/ai-observatory','European AI Observatory: Call for tenders','Procurement.',None)
    assert fallback_analysis(post,SimpleNamespace(priority='NORMAL',categories=[],type='WEBSITE')).category=='TENDER'
