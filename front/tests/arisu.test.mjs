import test from "node:test";
import assert from "node:assert/strict";
import { arisuNow, arisuStatus, arisuTime } from "../src/pages/Detail/data.js";

test("missing and unfamiliar Arisu statuses never become a safe verdict", () => {
  assert.deepEqual(arisuStatus(null), { text: "—", tone: "unknown" });
  assert.deepEqual(arisuStatus(" "), { text: "—", tone: "unknown" });
  assert.deepEqual(arisuStatus("안전"), { text: "안전", tone: "ok" });
  assert.deepEqual(arisuStatus("주의"), { text: "주의", tone: "warn" });
  for (const text of ["계량기 장애", "계량기장애", "단말기장애", "통신 오류", "수신오류", "계량기 고장으로 검침 자료가 없습니다."]) {
    assert.deepEqual(arisuStatus(text), { text, tone: "alert" });
  }
  assert.deepEqual(arisuStatus("통신상태 확인 중"), { text: "통신상태 확인 중", tone: "unknown" });
});

test("Arisu status carries its own time: card update for leak risk and usage, last reading for the meter, and when we checked", () => {
  const arisu = { leak_status: "주의", meter_status: null, realtime_usage: 0, updated_at: "2026-10-24 00:00", collected_at: "2026-10-24T12:00:05.1+09:00", readings: [
    { date: "2026-10-22", measured_at: "2026-10-23 00:00", status: "통신 장애" },
    { date: "2026-10-23", measured_at: "2026-10-24 00:00", status: "정상" },
  ] };
  assert.deepEqual(arisuNow(arisu), { leak: { text: "주의", tone: "warn" }, cardAt: "10/24 00:00", usage: 0,
    meter: { text: "정상", tone: "ok" }, meterAt: "10/24 00:00", checkedAt: "10/24 12:00" });
  // A card replaced by a fault notice: the notice is the meter status, and neither leak risk nor usage is guessed.
  const notice = "계량기 장애로 인해 원격검침 정보를 제공할 수 없습니다.";
  const fault = arisuNow({ ...arisu, leak_status: null, meter_status: notice, notice, realtime_usage: null, updated_at: null });
  assert.deepEqual([fault.leak.text, fault.usage, fault.meter, fault.meterAt], ["—", null, { text: notice, tone: "alert" }, ""]);
  assert.equal(arisuNow({ notice: "원격검침 데이터가 없습니다." }).leak.text, "원격검침 데이터가 없습니다.");
  for (const realtime_usage of [undefined, "4", NaN, Infinity]) assert.equal(arisuNow({ realtime_usage }).usage, null);
  assert.deepEqual(arisuNow(undefined), { leak: { text: "—", tone: "unknown" }, cardAt: "", usage: null, meter: { text: "—", tone: "unknown" }, meterAt: "", checkedAt: "" });
  for (const value of [null, undefined, "", "2026-10-24", "10/24 00:00", 123]) assert.equal(arisuTime(value), "");
});

test("detail opens on the last verdict, keeps warnings, and shows Arisu status in its own card with its time", async () => {
  const { createServer } = await import("vite");
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({ server: { middlewareMode: true, ws: false, watch: null }, appType: "custom" });
  try {
    const { default: DetailPage } = await server.ssrLoadModule("/src/pages/Detail/DetailPage.jsx");
    const data = {
      meters: [{ id: "a", customer_number: "000000001", station_id: "s", station_name: "테스트역", line: "1", provider: "arisu", active: 1, daily_enabled: 1 }],
      daily: { a: { usage: [{ date: "2026-10-21", value: 4 }, { date: "2026-10-23", value: 4 }] } },
      risk: { a: [{ date: "2026-10-21", pred: 5, actual: 4, residual: -1, severity: "정상" }, { date: "2026-10-23", pred: 9, actual: 4, residual: -5, severity: "주의" }] },
      arisu: { a: { leak_status: "주의", meter_status: null, realtime_usage: 4, updated_at: "2026-10-24 00:00", collected_at: "2026-10-24T12:00:05+09:00", readings: [
        { date: "2026-10-22", measured_at: "2026-10-23 00:00", status: "통신 장애" },
        { date: "2026-10-23", measured_at: "2026-10-24 00:00", status: "정상" },
      ] } },
      status: { latest_risk: "2026-10-23", reference_date: "2026-10-23" },
    };
    const original = JSON.stringify(data);
    const render = (hash = "#/detail?meter=a", snapshot = data) => renderToStaticMarkup(createElement(DetailPage, { data: snapshot, hash }));

    const current = render();
    assert.match(current, /10\/23 \(금\) 기준/);
    const stats = current.match(/<dl class="dt-risk-stats">(.*?)<\/dl>/)[1];
    assert.deepEqual([...stats.matchAll(/<dt>(.*?)<\/dt>/g)].map((match) => match[1]), ["예측", "실제", "오차", "예측 대비"]);
    assert.doesNotMatch(current, /물샘이|수집중/);
    assert.match(current, /<strong>-56%<\/strong><span>예측 대비<\/span>/);
    // Arisu's own status gets its own card above the verdict, with the source time and when it was checked.
    // Its realtime figure appears only there, never in the daily usage history.
    const arisuCard = (markup) => {
      const match = markup.match(/<h2>아리수 원격검침\(실시간\)<\/h2>(?:<div class="section-title-right">(.*?)<\/div>)?<\/div><dl class="dt-risk-stats dt-arisu-stats">(.*?)<\/dl>/);
      return match && { right: match[1], stats: [...match[2].matchAll(/<dt>(.*?)<\/dt><dd>(.*?)<\/dd>/g)].map((m) => [m[1], m[2]]) };
    };
    assert.deepEqual(arisuCard(current), { right: "10/24 00:00 기준, 10/24 12:00 확인", stats: [
      ["누수위험", '<span class="dt-arisu-value is-warn">주의</span>'],
      ["실시간 사용량", "4 톤"],
      ["계량기 상태", '<span class="dt-arisu-value is-ok">정상</span>'],
    ] });
    assert.equal((current.match(/실시간 사용량/g) || []).length, 1);
    assert.ok(current.indexOf("아리수 원격검침(실시간)") < current.indexOf("<h2>위험도</h2>"));
    assert.match(current, /class="dt-cal-day is-warn" aria-pressed="true" aria-label="10월 23일 주의"/);
    assert.match(current, /class="dt-cal-day is-empty is-meter-alert" aria-pressed="false" aria-label="10월 22일 판정 없음, 아리수 통신 장애"/);
    assert.match(current, /class="dt-cal-day is-empty" disabled="" aria-pressed="false" aria-label="10월 24일 판정 없음"/);
    assert.match(current.match(/<p class="dt-cal-legend">(.*?)<\/p>/)[1], /자료 확인.*계량기 이상.*판정 없음/);
    assert.match(current, /위험도는 매일 08시 수집이 끝나면 전날 사용량으로 판정합니다/);
    assert.doesNotMatch(current, /dt-risk-reading/);

    const faultDay = render("#/detail?meter=a&date=2026-10-22");
    assert.match(faultDay, /2026-10-22에는 위험도 분석 결과가 없습니다/);
    assert.match(faultDay, /<p class="dt-risk-reading">계량기 상태: 통신 장애<\/p>/);
    assert.match(render("#/detail?meter=a&date=2026-10-24"), /아직 판정 전입니다/);

    // The last day is a data check: its warning must lead the page, not a blank verdict for today.
    const stalled = render(undefined, { ...data, risk: { a: [{ date: "2026-10-23", pred: null, actual: 0, residual: null, err: true, reason: "값 그대로", since: "2026-09-01", days: 53, usual: 17.5 }] } });
    assert.match(stalled, /부터 53일째 그대로라\(매일 0톤\) 위험도를 판정하지 않습니다/);
    assert.match(stalled, /<strong>53일째<\/strong><span>원격검침 값 그대로<\/span>/);
    assert.match(stalled, /class="dt-cal-day is-unknown" aria-pressed="true" aria-label="10월 23일 자료 확인"/);
    assert.match(stalled, /<p class="dt-risk-head"><span class="risk-badge risk-unknown">자료 확인<\/span> 원격검침 값이 그대로라/);

    const faultCard = arisuCard(render(undefined, { ...data, arisu: { a: { ...data.arisu.a, leak_status: null, meter_status: "계량기 장애", realtime_usage: null, updated_at: null } } }));
    assert.deepEqual(faultCard, { right: "10/24 12:00 확인", stats: [
      ["누수위험", '<span class="dt-arisu-value is-unknown">—</span>'],
      ["실시간 사용량", "—"],
      ["계량기 상태", '<span class="dt-arisu-value is-alert">계량기 장애</span>'],
    ] });
    // A last reading older than the card gets its own time, so a stale meter status cannot pass for today's.
    const staleReading = arisuCard(render(undefined, { ...data, arisu: { a: { ...data.arisu.a, readings: [{ date: "2026-10-21", measured_at: "2026-10-22 00:00", status: "정상" }] } } }));
    assert.equal(staleReading.stats[2][1], '<span class="dt-arisu-value is-ok">정상</span><small>10/22 00:00 기준</small>');
    assert.equal(arisuCard(render(undefined, { ...data, arisu: {} })), null);
    assert.equal(JSON.stringify(data), original);
  } finally {
    await server.close();
  }
});
