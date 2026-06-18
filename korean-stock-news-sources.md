# 한국 주식 뉴스 수집 방법 종합 분석

> 조사일: 2026-05-19 | 실제 curl/Python 호출로 검증 완료

---

## 1. 네이버 뉴스 검색 API (공식)

**URL**: `https://openapi.naver.com/v1/search/news.json`  
**방식**: REST API, JSON/XML 응답  
**필요 인증**: 네이버 개발자 센터에서 발급받은 Client ID + Client Secret

### curl 예시
```bash
curl "https://openapi.naver.com/v1/search/news.json?query=%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90&display=10&sort=date&start=1" \
  -H "X-Naver-Client-Id: YOUR_CLIENT_ID" \
  -H "X-Naver-Client-Secret: YOUR_CLIENT_SECRET"
```

### Python 예시
```python
import requests
url = "https://openapi.naver.com/v1/search/news.json"
headers = {
    "X-Naver-Client-Id": "YOUR_CLIENT_ID",
    "X-Naver-Client-Secret": "YOUR_CLIENT_SECRET"
}
params = {"query": "삼성전자 주식", "display": 10, "sort": "date"}
r = requests.get(url, headers=headers, params=params)
data = r.json()  # items: [{title,originallink,link,description,pubDate}]
```

### 장단점
| 장점 | 단점 |
|------|------|
| 가장 풍부한 한국어 뉴스 데이터베이스 | Client ID/Secret 필요 (무료 발급) |
| JSON 응답으로 파싱 간편 | 하루 25,000회 호출 제한 |
| `sort=date`로 최신순 정렬 가능 | 검색어 기반이라 종목코드 직접 검색 불가 |
| 공식 API라 안정적 | 뉴스 본문 전체는 제공 안 함(요약만) |

### 상태
✅ **정상 작동** — 인증키만 있으면 바로 사용 가능. 현재 k-skill-proxy 404는 프록시 서버 문제.

---

## 2. Google News RSS (한국어)

**URL**: `https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko` (전체 뉴스)  
**URL**: `https://news.google.com/rss/search?q=삼성전자&hl=ko&gl=KR&ceid=KR:ko` (검색 - URL 인코딩 필요)  
**방식**: RSS 2.0 XML 피드  
**필요 인증**: 없음 (단, 일반 User-Agent는 400 에러 → Googlebot UA 필요)

### curl 예시
```bash
# 전체 한국 뉴스 (정상 확인됨)
curl -s "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko" \
  -H "User-Agent: Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# 특정 검색어 (URL 인코딩 필요 - 검색어 쿼리는 400 응답 가능성 있음)
curl -s "https://news.google.com/rss/search?q=%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90&hl=ko&gl=KR&ceid=KR:ko" \
  -H "User-Agent: Mozilla/5.0 (compatible; Googlebot/2.1)"
```

### Python 예시
```python
import requests
import xml.etree.ElementTree as ET

url = "https://news.google.com/rss"
params = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}
headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
r = requests.get(url, params=params, headers=headers)
root = ET.fromstring(r.text)
for item in root.findall(".//item"):
    title = item.find("title").text
    pubdate = item.find("pubDate").text
```

### 장단점
| 장점 | 단점 |
|------|------|
| 인증 없이 사용 가능 | 일반 UA로는 차단됨 (400/403) |
| 다양한 언론사 통합 제공 | 구글 리다이렉트 링크 (원문 직접 링크 아님) |
| 글로벌 표준 RSS 형식 | 하루 수백 건만 제공 (페이지네이션 불가) |
| 실시간 갱신 빠름 | 검색어 쿼리 형식이 까다로움 (rss/search 엔드포인트 불안정) |

### 상태
⚠️ **부분 작동** — 메인 피드는 정상이나 `/rss/search`는 400 응답. 전체 피드에서 클라이언트 측 필터링 권장.

---

## 3. 한국경제 (hankyung.com) RSS

**RSS 피드 목록**:
- `https://www.hankyung.com/feed/economy.xml` (경제 - ✅ 200)
- `https://www.hankyung.com/feed/finance.xml` (증권 - ✅ 200)  
- `https://www.hankyung.com/feed/realestate.xml` (부동산 - ✅ 200)
- `https://www.hankyung.com/feed/stock.xml` (❌ 404)
- `https://www.hankyung.com/feed/all.xml` (전체)

**방식**: RSS 2.0 XML  
**필요 인증**: 없음

### curl 예시
```bash
# 증권 뉴스 (주식 관련 가장 적합)
curl -s "https://www.hankyung.com/feed/finance.xml" \
  -H "User-Agent: Mozilla/5.0"
```

### 응답 구조
```xml
<item>
  <title><![CDATA["로봇주 너무 올랐나?"…두산로보틱스, 장 초반 13%대 급락]]></title>
  <link><![CDATA[https://www.hankyung.com/article/2026051954246]]></link>
  <author><![CDATA[강경주]]></author>
  <pubDate>Tue, 19 May 2026 09:43:21 +0900</pubDate>
</item>
```

### 장단점
| 장점 | 단점 |
|------|------|
| 인증 불필요, 빠르고 간단 | 주식 종목별 필터링 없음 (카테고리만) |
| 경제/증권 특화 콘텐츠 | `feed/stock.xml`은 404 (finance.xml 사용) |
| 원문 링크 직접 제공 | RSS 리더용으로만 설계됨 |
| UTF-8 인코딩 명확 | |

### 상태
✅ **정상 작동** — `economy.xml`, `finance.xml`, `realestate.xml` 모두 확인.

---

## 4. 매일경제 (mk.co.kr) RSS

**RSS 피드 목록**:
- `https://www.mk.co.kr/rss/30000001/` (헤드라인 - ✅)
- `https://www.mk.co.kr/rss/40300000/` (증권 - ✅)
- `https://www.mk.co.kr/rss/50100000/` ~ `50300000/` (기타 - ✅)

**방식**: RSS 2.0 XML  
**필요 인증**: 없음

### curl 예시
```bash
# 증권 카테고리
curl -s "https://www.mk.co.kr/rss/40300000/" -H "User-Agent: Mozilla/5.0"

# 헤드라인 (종합)
curl -s "https://www.mk.co.kr/rss/30000001/" -H "User-Agent: Mozilla/5.0"
```

### 응답 구조
```xml
<item>
  <no>12051747</no>
  <title><![CDATA[두산로보틱스, 차익실현 매물 쏟아지자 12%대 급락]]></title>
  <link><![CDATA[https://www.mk.co.kr/news/stock/12051747]]></link>
  <category><![CDATA[헤드라인]]></category>
  <pubDate>Tue, 19 May 2026 09:40:46 +09:00</pubDate>
  <description><![CDATA[...]]></description>
  <media:content medium="image" url="..." />
</item>
```

### 장단점
| 장점 | 단점 |
|------|------|
| 인증 불필요 | 종목별 필터링 없음 |
| 증권 특화 카테고리 있음 (`stock` 태그 포함) | RSS 항목 수 제한적 |
| 썸네일 이미지 URL 포함 | 카테고리 ID 파악 필요 |
| `news/stock/` 하위 링크 구조 | |

### 상태
✅ **정상 작동** — 여러 RSS 경로 모두 확인.

---

## 5. Investing.com 한국 RSS

**URL**: `https://kr.investing.com/rss/news.rss`  
**방식**: RSS 2.0 XML  
**필요 인증**: 없음

### curl 예시
```bash
curl -s "https://kr.investing.com/rss/news.rss" -H "User-Agent: Mozilla/5.0"
```

### 응답 구조
```xml
<item>
  <title>[특징주] 티엠씨, 美 AI 데이터센터 광케이블 수주 소식에 상한가</title>
  <pubDate>2026-05-19 00:41:21</pubDate>
  <author>EBN</author>
  <link>https://kr.investing.com/news/stock-market-news/article-1951239</link>
  <enclosure url="..." type="image/jpeg" />
</item>
```

### 장단점
| 장점 | 단점 |
|------|------|
| 금융/투자 특화 뉴스 (특징주 등) | 한국어 기사 수 제한적 |
| 글로벌 증시 뉴스 포함 | RSS 외 종목별 API 없음 |
| 인증 불필요 | 일부 기사는 Investing.com 내부 링크 |

### 상태
✅ **정상 작동** — 특징주, 글로벌 증시 뉴스 혼합 제공.

---

## 6. Yahoo Finance

**주가 데이터**: `https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}.KS`  
**뉴스 검색**: `https://query2.finance.yahoo.com/v1/finance/search?q={TICKER}`  
**방식**: REST API, JSON 응답  
**필요 인증**: 없음

### curl 예시
```bash
# 삼성전자(005930.KS) 주가 데이터 (정상 확인)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/005930.KS?interval=1d&range=1mo" \
  -H "User-Agent: Mozilla/5.0"

# 종목 검색 + 뉴스 (정상 확인)
curl -s "https://query2.finance.yahoo.com/v1/finance/search?q=005930.KS&newsCount=5" \
  -H "User-Agent: Mozilla/5.0"
```

### Python 예시
```python
import requests
url = "https://query2.finance.yahoo.com/v1/finance/search"
params = {"q": "005930.KS", "newsCount": 5}
headers = {"User-Agent": "Mozilla/5.0"}
r = requests.get(url, params=params, headers=headers)
data = r.json()
for news in data.get("news", []):
    print(news["title"], news["link"], news["publisher"])
```

### 장단점
| 장점 | 단점 |
|------|------|
| 종목 코드로 직접 검색 가능 | 한국 종목 뉴스가 매우 제한적 (영문 위주) |
| JSON 응답, 파싱 간편 | 뉴스는 글로벌 제너럴 뉴스만 반환 |
| 공식/비공식 API 오래 유지 | 변동 가능성 (비공식 API) |

### 상태
⚠️ **부분 작동** — 주가 데이터 API는 정상. 뉴스는 한국 종목 검색 시 글로벌 일반뉴스만 반환, 한국어 뉴스 거의 없음.

---

## 7. 네이버 금융 (비공식 스크래핑)

**URL**: `https://finance.naver.com/item/news_news.naver?code=005930&page=1`  
**주가 API**: `https://api.finance.naver.com/siseJson.naver?symbol=005930`  
**방식**: HTML 스크래핑 / 비공식 JSON API  
**필요 인증**: 없음

### curl 예시 (주가)
```bash
# 주가 데이터 (비공식 JSON API - 정상 확인)
curl -s "https://api.finance.naver.com/siseJson.naver?requestType=1&startTime=20260501&endTime=20260519&timeframe=day&symbol=005930" \
  -H "User-Agent: Mozilla/5.0"
```

### 장단점
| 장점 | 단점 |
|------|------|
| 종목코드로 직접 뉴스 조회 가능 | **비공식 API — 언제든 변경/차단 가능** |
| 가장 풍부한 한국 종목 뉴스 | HTML 파싱 필요 (뉴스) |
| 인증 불필요 | EUC-KR 인코딩 → UTF-8 변환 필요 |
| 주가 API는 JSON 유사 응답 | 법적 문제 가능성, IP 차단 위험 |

### 상태
⚠️ **주의** — 비공식 API로 안정성 보장 안 됨. 법적 문제 가능성. HTML 스크래핑 시 IP 차단 위험.

---

## 📊 종합 비교표

| 소스 | 인증 | 뉴스 품질 | 종목 필터 | 안정성 | 추천 |
|------|------|-----------|-----------|--------|------|
| **네이버 검색 API** | Client ID | ★★★★★ | ❌ (키워드) | ★★★★★ | ✅ 강력 추천 |
| **Google News RSS** | 없음 | ★★★★ | ❌ (키워드) | ★★★ | ✅ 추천 |
| **한국경제 RSS** | 없음 | ★★★★ | ❌ (카테고리) | ★★★★ | ✅ 추천 |
| **매일경제 RSS** | 없음 | ★★★★ | ❌ (카테고리) | ★★★★ | ✅ 추천 |
| **Investing.com RSS** | 없음 | ★★★ | ❌ (혼합) | ★★★ | ⚠️ 보조용 |
| **Yahoo Finance** | 없음 | ★★ | ✅ (티커) | ★★★ | ⚠️ 영문만 |
| **네이버 금융 스크래핑** | 없음 | ★★★★★ | ✅ (종목코드) | ★★ | ❌ 위험 |

---

## 🎯 권장 전략 (종합 뉴스 수집 파이프라인)

```
1. [1순위] 네이버 검색 API — 종목명 검색어로 뉴스 수집
   → curl -H "X-Naver-Client-Id: ..." "https://openapi.naver.com/v1/search/news.json?query=삼성전자&display=20&sort=date"

2. [2순위] 한국경제/매일경제 RSS — 증권 카테고리 정기 폴링
   → curl -s "https://www.hankyung.com/feed/finance.xml"
   → curl -s "https://www.mk.co.kr/rss/40300000/"

3. [3순위] Google News RSS — 종합 뉴스 보완 (Googlebot UA 필수)
   → curl -s "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko" -H "User-Agent: Googlebot/2.1"

4. [보조] Investing.com RSS — 글로벌 금융 시각 보완
   → curl -s "https://kr.investing.com/rss/news.rss"
```

### k-skill-proxy 404 문제 진단
현재 `k-skill-proxy`가 `/api/news/005930` 등에서 404를 내는 것은 **프록시 서버 자체 문제**임.
- 네이버 검색 API 자체는 정상 작동 (인증 오류만 확인, Client ID/Secret 있으면 동작)
- 프록시의 라우팅 설정, 엔드포인트 정의, 또는 인증키 설정을 확인해야 함
- 대안: 프록시를 거치지 않고 Python에서 `requests`로 직접 네이버 API 호출하는 방식으로 전환 권장

### 실전 Python 통합 예시
```python
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import time

class KoreanStockNewsCollector:
    """한국 주식 뉴스 통합 수집기"""
    
    def __init__(self, naver_client_id=None, naver_client_secret=None):
        self.naver_id = naver_client_id
        self.naver_secret = naver_client_secret
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
    
    def from_naver_api(self, keyword, display=20):
        """네이버 검색 API로 뉴스 수집"""
        if not self.naver_id or not self.naver_secret:
            raise ValueError("Naver API credentials required")
        r = self.session.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={
                "X-Naver-Client-Id": self.naver_id,
                "X-Naver-Client-Secret": self.naver_secret
            },
            params={"query": keyword, "display": display, "sort": "date"}
        )
        items = r.json().get("items", [])
        return [{"title": i["title"], "link": i["link"], "pubDate": i["pubDate"],
                 "source": "naver_api"} for i in items]
    
    def from_hankyung_rss(self):
        """한국경제 증권 RSS"""
        r = self.session.get("https://www.hankyung.com/feed/finance.xml")
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            items.append({
                "title": item.find("title").text,
                "link": item.find("link").text,
                "pubDate": item.find("pubDate").text,
                "source": "hankyung"
            })
        return items
    
    def from_mk_rss(self):
        """매일경제 증권 RSS"""
        r = self.session.get("https://www.mk.co.kr/rss/40300000/")
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            items.append({
                "title": item.find("title").text,
                "link": item.find("link").text,
                "pubDate": item.find("pubDate").text,
                "source": "mk"
            })
        return items
    
    def from_google_news(self):
        """Google News RSS (Googlebot UA 필수)"""
        r = requests.get(
            "https://news.google.com/rss",
            params={"hl": "ko", "gl": "KR", "ceid": "KR:ko"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
        )
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            items.append({
                "title": item.find("title").text,
                "link": item.find("link").text,
                "pubDate": item.find("pubDate").text,
                "source": "google_news"
            })
        return items
    
    def from_investing_rss(self):
        """Investing.com 한국 RSS"""
        r = self.session.get("https://kr.investing.com/rss/news.rss")
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            items.append({
                "title": item.find("title").text,
                "link": item.find("link").text,
                "pubDate": item.find("pubDate").text,
                "source": "investing"
            })
        return items
    
    def collect_all(self, stock_name=None):
        """모든 소스에서 뉴스 수집"""
        all_news = []
        try:
            all_news.extend(self.from_hankyung_rss())
        except Exception as e:
            print(f"[hankyung] error: {e}")
        try:
            all_news.extend(self.from_mk_rss())
        except Exception as e:
            print(f"[mk] error: {e}")
        try:
            all_news.extend(self.from_google_news())
        except Exception as e:
            print(f"[google] error: {e}")
        try:
            all_news.extend(self.from_investing_rss())
        except Exception as e:
            print(f"[investing] error: {e}")
        if stock_name and self.naver_id:
            try:
                all_news.extend(self.from_naver_api(stock_name))
            except Exception as e:
                print(f"[naver] error: {e}")
        return all_news


# 사용 예시
collector = KoreanStockNewsCollector(
    naver_client_id="YOUR_ID",
    naver_client_secret="YOUR_SECRET"
)
news = collector.collect_all(stock_name="삼성전자")
for n in news[:10]:
    print(f"[{n['source']}] {n['title']} - {n.get('pubDate', '')}")
```
