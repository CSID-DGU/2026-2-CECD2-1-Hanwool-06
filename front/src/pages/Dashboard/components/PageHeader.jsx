// 화면 제목과 현황 한 줄.
export default function PageHeader({ daily, alerts, warnings, dataChecks = 0 }) {
  return (
    <section className="page-head">
      <div>
        <h1>상수도 관제 현황</h1>
        <p className="head-summary">
          {daily
            ? <>일일 관제 계량기 {daily}개 가운데 <strong className={alerts ? "is-alert" : ""}>경고 {alerts}건</strong>, <strong className={warnings ? "is-warn" : ""}>주의 {warnings}건</strong>{dataChecks ? `, 자료 확인 ${dataChecks}건` : ""}입니다.</>
            : "일일 사용량을 수집하는 계량기가 아직 없습니다."}
        </p>
      </div>
    </section>
  );
}
