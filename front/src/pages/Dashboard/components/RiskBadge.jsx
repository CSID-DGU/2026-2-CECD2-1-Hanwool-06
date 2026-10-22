import { riskMeta } from "../dashboardData.js";

export default function RiskBadge({ risk, label }) {
  const meta = riskMeta[risk] || riskMeta.unknown;
  return <span className={`risk-badge risk-${meta.tone}`}>{label || meta.label}</span>;
}
