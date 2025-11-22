import os
import json
import logging
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from lxml import etree

# ----------------------------
# 1. 기본 설정
# ----------------------------
load_dotenv() # .env 파일 로드

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS 설정: 로컬 테스트 편의를 위해 모든 출처 허용
CORS(app, resources={r"/api/*": {"origins": "*"}})

# === API 키 ===
DART_API_KEY = os.getenv('DART_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-preview-09-2025')

# 뉴스 검색용 네이버 API 키
NAVER_CLIENT_ID = os.getenv('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.getenv('NAVER_CLIENT_SECRET')

# HTTP 세션 설정 (재시도 로직)
session = requests.Session()
session.headers.update({'User-Agent': 'portfolio-backend/1.0'})
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
        logger.warning("CORPCODE.xml 파일이 없습니다. 검색 기능이 제한됩니다.")
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

# ----------------------------
# 4. [핵심] 웹페이지 보여주기
# ----------------------------
@app.route('/')
def home():
    try:
        return send_file('index.html')
    except Exception as e:
        return f"<h3>index.html 파일을 찾을 수 없습니다.</h3><p>{e}</p>"

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
            # 2차 복구
            res2 = call_gemini(f"Fix JSON:\n{text}")
            return jsonify(json.loads(extract_json(collect_text(res2.json()))))
    except Exception as e:
        logger.error(f"분석 에러: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/news-summary', methods=['POST'])
def news_summary():
    data = request.get_json()
    keyword = data.get('keyword')
    news_items = []
    
    # 1. 네이버 API 호출
    if NAVER_CLIENT_ID and NAVER_CLIENT_SECRET:
        try:
            url = "https://openapi.naver.com/v1/search/news.json"
            headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
            res = session.get(url, headers=headers, params={"query": keyword, "display": 5, "sort": "sim"})
            
            if res.status_code == 200:
                items = res.json().get('items', [])
                for item in items:
                    news_items.append({
                        "title": item['title'],
                        "description": item['description'],
                        "link": item['link'],
                        "pubDate": item['pubDate']
                    })
            else:
                logger.error(f"네이버 API 오류: {res.status_code}")
        except Exception as e:
            logger.error(f"네이버 API 연동 실패: {e}")

    # 2. [수정] 뉴스 데이터가 없을 경우 (가짜 뉴스 생성 로직 제거됨)
    if not news_items:
        return jsonify({
            'news_list': [],
            'ai_summary': f"<b>'{keyword}'에 대한 최근 뉴스 검색 결과가 없습니다.</b><br>기업명을 다시 확인하거나, 네이버 API 키 설정을 확인해주세요."
        })

    # 3. Gemini 요약 (뉴스가 있을 때만 실행)
    summary = "요약에 실패했습니다."
    try:
        news_text = "\n".join([
            f"{i+1}. {n['title'].replace('<b>','').replace('</b>','')} : {n['description'].replace('<b>','').replace('</b>','')}" 
            for i, n in enumerate(news_items)
        ])
        
        prompt = f"다음 '{keyword}' 관련 뉴스들을 취업 면접 대비용으로 3줄로 핵심 요약해줘. HTML 태그(<ul>, <li>, <b>)를 사용해서 가독성 있게 출력해줘:\n{news_text}"
        
        res = call_gemini(prompt)
        if res.status_code == 200:
            summary = collect_text(res.json()).replace('```html', '').replace('```', '').strip()
    except Exception as e:
        logger.error(f"요약 생성 에러: {e}")

    return jsonify({
        'news_list': news_items,
        'ai_summary': summary
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)
    
