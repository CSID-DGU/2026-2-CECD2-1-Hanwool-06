"""User-triggered alerts to assigned office managers, and the daily collection failure notice to superadmins."""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from . import config


def send_mail(subject, body_html, to=None):
    if not config.smtp_ready():
        return {'sent': False, 'reason': '서버에 메일(SMTP) 설정이 없어 보내지 못했습니다.'}
    recipients = [a.strip() for a in (to or '').split(',') if a.strip()]
    if not recipients:
        return {'sent': False, 'reason': '이 사업소에는 메일을 받을 담당자가 없습니다.'}
    msg = MIMEText(body_html, 'html', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr(('물샘이 (상수도 이상징후 관제)', config.ALERT_FROM))
    msg['To'] = ', '.join(recipients)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(config.SMTP_USER, config.SMTP_PASS)
            refused = smtp.sendmail(config.ALERT_FROM, recipients, msg.as_string())
        return {'sent': not refused, 'to': [r for r in recipients if r not in refused],
                **({'reason': '일부 담당자에게는 보내지 못했습니다.'} if refused else {})}
    except (smtplib.SMTPException, OSError):
        return {'sent': False, 'reason': '메일을 보내지 못했습니다. 서버의 메일 설정을 확인하세요.'}


def build_alert_html(item):
    def safe(value):
        return escape(str(value if value is not None else '미확인'))
    rows = ''.join(f'<tr><th>{safe(k)}</th><td>{safe(v)}</td></tr>' for k, v in [
        ('역명', item.get('역명')), ('사업소', item.get('영업사업소')), ('고객번호', item.get('고객번호')),
        ('기준일', item.get('날짜')), ('심각도', item.get('심각도')), ('실제 사용량(톤)', item.get('actual_ton')),
        ('예측 사용량(톤)', item.get('predicted_ton')), ('예측 대비(%)', item.get('pct'))])
    return f'<html><body><h2>상수도 이상징후 확인 요청</h2><table>{rows}</table><p>관제 화면에서 담당자가 보낸 알림입니다. 원인은 현장에서 확인해야 합니다.</p></body></html>'


def build_collection_alert_html(alert):
    labels = {'failed': '실패', 'partial': '일부 실패', 'interrupted': '중단', 'configuration_required': '설정 필요'}
    causes = {}
    for item in alert['items']:
        causes.setdefault((labels.get(item['status'], item['status']), item['message']), []).append(
            f"{item['display_name']}({item['customer_number']})")
    rows = ''.join(f'<tr><th>{escape(label)} {len(names)}곳</th><td>{escape(message)}<br>{escape(", ".join(names))}</td></tr>'
                   for (label, message), names in causes.items())
    retried = f'과 30분 간격 재시도 {alert["retries"]}번' if alert['retries'] else ''
    return (f'<html><body><h2>아리수 자동 수집 실패</h2><p>{escape(alert["day"])} 08:00 자동 수집{retried}이 끝났지만 '
            f'아래 {len(alert["items"])}곳은 자료를 다 받지 못했습니다. 받은 자료와 기존 자료는 그대로이고, 빠진 날짜는 다음 수집 때 다시 조회합니다. '
            '급하면 관제 화면의 정보 업데이트로 바로 다시 수집할 수 있습니다. 로그인·설정·자료 오류는 계정 잠김을 막으려고 자동으로 다시 시도하지 않습니다.</p>'
            f'<table>{rows}</table></body></html>')
