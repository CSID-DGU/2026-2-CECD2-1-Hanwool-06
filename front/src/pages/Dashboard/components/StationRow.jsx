import LineBadges from "./LineBadges.jsx";
import RiskBadge from "./RiskBadge.jsx";
import { detailHref, signedPct } from "../../Detail/data.js";
import { meterName } from "../dashboardData.js";

// byMeter: 한 역 안의 계량기를 늘어놓을 때. 계량기 이름(초기 자료의 "역명+호선"은 역명만)과 고객번호로 구분한다.
export default function StationRow({ station, byMeter = false }) {
  return (
    <a href={detailHref(station.id)} className="station-row">
      <LineBadges lines={station.lines} />
      <span className="station-row-name">
        <strong>{byMeter ? meterName(station) : station.name}</strong>
        <small>{byMeter ? `고객번호 ${station.customerNo}` : station.office}</small>
        {/* 자료 확인인 까닭(예: 원격검침 값 81일째 그대로). 표의 위험도 칸과 같은 문구다. */}
        {station.riskDetail && <small className="station-row-note">{station.riskDetail}</small>}
      </span>
      <span className="station-row-side">
        <RiskBadge risk={station.risk} />
        <b>{signedPct(station.delta)}</b>
      </span>
    </a>
  );
}
