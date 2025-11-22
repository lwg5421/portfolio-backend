# app.py (네이버 API 키 없이 크롤링으로 뉴스 가져오는 버전)
import os
import json
import logging
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from lxml import etree
from bs4 import BeautifulSoup # [필수] 크롤링을 위해 추가됨

# ----------------------------
# 1. 기본 설정
# ----------------------------
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS 설정
CORS(app, resources={r"/api/*": {"origins": "*"}})

# === API 키 ===
DART_API_KEY = os.getenv('DART_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-preview-09-2025')

# 네이버 API 키는 더 이상 필요 없습니다! (삭제함)

# HTTP 세션 설정
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
})
retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.mount('http://', HTTPAdapter(max_retries=retries))

# ----------------------------
# 2. 데이터 로드 (XML)
# ----------------------------
CORP_XML_PATH = 'CORPCODE.xml'
corp_name_map = {}

try:
    if os.path.exists(CORP_XML_PATH):
        logger.info(f"파일 로드 중: {CORP_XML_PATH}")
        context = etree.iterparse(CORP_XML_PATH, events=('end',), tag='list')
        for event, elem in context:
            c_name = elem.findtext('corp_name')
            c_code = elem.findtext('corp_code')
            if c_name and c_code:
                clean_name = c_name.replace('(주)', '').strip()
                corp_name_map[clean_name] = {"code": c_code, "original_name": c_name}
            elem.clear()
        del context
        logger.info(f"기업 정보 로드 완료: {len(corp_name_map)}개")
    else:
        logger.warning("CORPCODE.xml 파일이 없습니다.")
except Exception as e:
    logger.error(f"XML 로드 에러: {e}")

# ----------------------------
# 3. 헬퍼 함수들
# ----------------------------
DART_API_URL = 'https://opendart.fss.or.kr/api'
GEMINI_URL_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

def dart_get(path, params):
    if not DART_API_KEY: return {}
    params['crtfc_key'] = DART_API_KEY
    res = session.get(f"{DART_API_URL}/{path}", params=params, timeout=15)
    res.raise_for_status()
    return res.json()

def call_gemini(prompt):
    if not GEMINI_API_KEY: return requests.Response()
    url = f"{GEMINI_URL_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096, "responseMimeType": "application/json"}
    }
    return session.post(url, json=payload, timeout=60)

def collect_text(gemini_res):
    texts = []
    for cand in gemini_res.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"): texts.append(part["text"])
    return "\n".join(texts).strip()

def extract_json(text):
    if not text: return ""
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        return text[start:end+1]
    return ""

# [NEW] 네이버 뉴스 크롤링 함수
def crawl_naver_news(keyword):
    """네이버 뉴스 검색 결과를 크롤링합니다."""
    base_url = "https://search.naver.com/search.naver"
    params = {
        "where": "news",
        "query": keyword,
        "sort": "0", # 관련도순
        "photo": "0",
        "field": "0",
        "pd": "0",
        "ds": "",
        "de": "",
        "cluster_rank": "1",
        "mynews": "0",
        "office_type": "0",
        "office_section_code": "0",
        "news_office_checked": "",
        "nso": "so:r,p:all,a:all",
        "start": "1"
    }
    
    try:
        response = session.get(base_url, params=params, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        news_list = []
        # 네이버 뉴스 리스트 선택자 (변경될 수 있음)
        items = soup.select("ul.list_news > li")
        
        for item in items[:5]: # 상위 5개만
            title_tag = item.select_one("a.news_tit")
            desc_tag = item.select_one("div.news_dsc")
            
            if title_tag and desc_tag:
                news_list.append({
                    "title": title_tag.get_text(),
                    "description": desc_tag.get_text(),
                    "link": title_tag['href'],
                    "pubDate": "최근" # 크롤링은 정확한 날짜 파싱이 복잡해서 생략
                })
        return news_list
    except Exception as e:
        logger.error(f"크롤링 실패: {e}")
        return []

# ----------------------------
# 4. 웹페이지 서빙
# ----------------------------
@app.route('/')
def home():
    try:
        return send_file('index.html')
    except Exception as e:
        return f"index.html 파일이 없습니다. {e}"

# ----------------------------
# 5. API 엔드포인트
# ----------------------------
@app.route('/api/search', methods=['GET'])
def search():
    name = request.args.get('name', '').strip()
    clean_name = name.replace('(주)', '').strip()
    if not name: return jsonify({'status': '400', 'message': '기업명을 입력하세요.'}), 400
    
    res = corp_name_map.get(clean_name)
    if res:
        return jsonify({'status': '000', 'corp_code': res['code'], 'corp_name': res['original_name']})
    return jsonify({'status': '404', 'message': '기업을 찾을 수 없습니다.'}), 404

@app.route('/api/company', methods=['GET'])
def company():
    code = request.args.get('code')
    try:
        return jsonify(dart_get('company.json', {'corp_code': code}))
    except Exception as e:
        return jsonify({'status': '500', 'message': str(e)}), 500

@app.route('/api/finance', methods=['GET'])
def finance():
    code = request.args.get('code')
    year = request.args.get('year')
    try:
        data = dart_get('fnlttSinglAcntAll.json', {'corp_code': code, 'bsns_year': year, 'reprt_code': '11014', 'fs_div': 'CFS'})
        if data.get('status') != '000' or not data.get('list'):
            data = dart_get('fnlttSinglAcntAll.json', {'corp_code': code, 'bsns_year': year, 'reprt_code': '11014', 'fs_div': 'OFS'})
        return jsonify(data)
    except Exception as e:
        return jsonify({'status': '500', 'message': str(e)}), 500

@app.route('/api/generate-analysis', methods=['POST'])
def analyze():
    data = request.get_json()
    name = data.get('name', '')
    biz = data.get('bizArea', '')
    
    schema = """
    {
      "vision": "기업 비전(한국어)",
      "productsAndServices": ["제품1", "제품2"],
      "performanceSummary": "실적 요약(한국어)",
      "swot": {"strength": [], "weakness": [], "opportunity": [], "threat": [], "strategy": "전략(한국어)"},
      "industryAnalysis": {"method": "", "result": "", "competitors": "", "competitorAnalysis": ""},
      "job": {"duties": "", "description": "", "knowledge": "", "skills": "", "attitude": "", "certs": "", "env": "", "careerDev": ""},
      "selfAnalysis": {"knowledge": "", "skills": "", "attitude": "", "actionPlan1": "", "actionPlan2": "", "actionPlan3": ""}
    }
    """
    prompt = f"기업 '{name}({biz})'을 프론트엔드 개발자 취업 준비생 관점에서 분석해줘. 아래 JSON 형식으로만 답해줘.\n{schema}"
    
    try:
        res = call_gemini(prompt)
        if res.status_code != 200: return jsonify({'error': 'Gemini Error', 'details': res.text}), 500
        
        text = collect_text(res.json())
        json_str = extract_json(text)
        if json_str:
            return jsonify(json.loads(json_str))
        else:
            res2 = call_gemini(f"Fix JSON:\n{text}")
            return jsonify(json.loads(extract_json(collect_text(res2.json()))))
    except Exception as e:
        logger.error(f"분석 에러: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/news-summary', methods=['POST'])
def news_summary():
    """크롤링을 이용한 뉴스 검색 및 AI 요약"""
    data = request.get_json()
    keyword = data.get('keyword')
    
    logger.info(f"뉴스 크롤링 요청: {keyword}")

    # 1. 크롤링으로 뉴스 가져오기
    news_items = crawl_naver_news(keyword)

    # 2. 뉴스가 없으면 안내 메시지
    if not news_items:
        return jsonify({
            'news_list': [],
            'ai_summary': f"<b>'{keyword}'에 대한 뉴스 검색 결과가 없습니다.</b><br>네이버 검색 페이지 구조가 변경되었거나, 검색어가 너무 특이할 수 있습니다."
        })

    # 3. Gemini 요약
    summary = "요약에 실패했습니다."
    try:
        news_text = "\n".join([f"{i+1}. {n['title']}: {n['description']}" for i, n in enumerate(news_items)])
        prompt = f"다음 '{keyword}' 관련 뉴스들을 취업 면접 대비용으로 3줄로 핵심 요약해줘. HTML 태그(<ul>, <li>, <b>)를 사용해서 가독성 있게 출력해줘:\n{news_text}"
        
        res = call_gemini(prompt)
        if res.status_code == 200:
            summary = collect_text(res.json()).replace('```html', '').replace('```', '').strip()
    except Exception as e:
        logger.error(f"요약 생성 에러: {e}")

    return jsonify({'news_list': news_items, 'ai_summary': summary})

if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)
