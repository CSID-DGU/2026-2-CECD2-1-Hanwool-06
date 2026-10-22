import { useLayoutEffect, useRef, useState } from "react";
import { chartRange, xLabelStep, xLabelVisible, yLabelChars } from "./data.js";

// 의존성 없는 SVG 라인차트.
// 컨테이너 실제 너비를 측정해 viewBox=픽셀 1:1 로 그림 → 폭이 달라도 글씨 크기·여백이 일정.
// series: [{ color, values: number[], fill?: bool }] / xLabels: string[]
// labelEvery: x라벨 최소 간격(라벨이 겹칠 만큼 좁으면 자동으로 더 벌린다) / showValues: 각 점 위 값 표기 / yUnit·xUnit: 축 단위
// labelChars: y축 숫자 칸을 적어도 이 글자 수만큼 잡는다. 나란히 놓은 그래프끼리 x축 길이를 맞출 때 쓴다(data.js yLabelChars).
export default function LineChart({
  series,
  xLabels = [],
  height = 170,
  formatY = (v) => v,
  formatValue,
  showValues = false,
  labelEvery,
  yUnit = "",
  xUnit = "",
  labelChars = 0,
}) {
  const wrapRef = useRef(null);
  const [W, setW] = useState(640);
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setW(Math.max(260, el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const range = chartRange(series.flatMap((s) => s.values), showValues);
  if (!range) return <div ref={wrapRef} className="empty-state">이 기간에는 자료가 없습니다.</div>;
  const { lo, hi } = range;
  const n = series[0].values.length;
  const yTicks = [hi, (lo + hi) / 2, lo];

  const H = height;
  // 왼쪽 여백은 가장 긴 축 숫자에 맞춘다. 금액처럼 긴 숫자도 잘리지 않는다.
  const longest = Math.max(labelChars, yLabelChars(series.flatMap((s) => s.values), formatY, showValues));
  const yLabelX = Math.max(44, 8 + longest * 7.5); // y축 숫자의 오른쪽 끝
  const padL = yLabelX + 28;
  const padR = 28;
  const padT = showValues || yUnit ? 28 : 16; // 단위 표기와 맨 위 눈금이 겹치지 않게
  const padB = 40;

  const X = (i) => padL + (W - padL - padR) * (n <= 1 ? 0.5 : i / (n - 1));
  const Y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));

  // null/결측은 선을 끊어서(gap) 그림 → 검침 미수집 구간 표현
  const line = (vals) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (!Number.isFinite(v)) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  };
  const areaPath = (vals) =>
    vals.some((v) => !Number.isFinite(v))
      ? "" // 결측이 있으면 면적 채움 생략(끊긴 구간 음영 방지)
      : `${line(vals)} L${X(n - 1).toFixed(1)},${Y(lo).toFixed(1)} L${X(0).toFixed(1)},${Y(lo).toFixed(1)} Z`;

  // 라벨은 글자 폭이 점 사이 간격에 들어갈 만큼만 찍고, 마지막 점에서 거꾸로 같은 간격으로 고른다(끝 라벨이 이웃과 붙지 않는다).
  const step = xLabelStep(xLabels, n <= 1 ? W : (W - padL - padR) / (n - 1), labelEvery);
  const fv = formatValue || formatY;

  return (
    <div ref={wrapRef} className="dt-chart-wrap">
      <svg className="dt-chart" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${xLabels[0] || ''}부터 ${xLabels.at(-1) || ''}까지 ${yUnit} 추이`}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={Y(t)} y2={Y(t)} className="dt-chart-grid" />
            <text x={yLabelX} y={Y(t) + 4} textAnchor="end" className="dt-chart-axis">
              {formatY(Math.round(t))}
            </text>
          </g>
        ))}
        {xLabels.map((lab, i) =>
          xLabelVisible(i, n, step) ? (
            <text key={i} x={X(i)} y={H - 24} textAnchor="middle" className="dt-chart-axis">
              {lab}
            </text>
          ) : null
        )}
        {yUnit ? (
          <text x={yLabelX} y={15} textAnchor="end" className="dt-chart-unit">
            {yUnit}
          </text>
        ) : null}
        {xUnit ? (
          <text x={W - 4} y={H - 6} textAnchor="end" className="dt-chart-unit">
            {xUnit}
          </text>
        ) : null}
        {series.map((s, si) => (s.fill ? <path key={`a${si}`} d={areaPath(s.values)} fill={s.color} opacity="0.08" /> : null))}
        {series.map((s, si) => (
          <path key={`l${si}`} d={line(s.values)} fill="none" stroke={s.color} strokeWidth="2.5" strokeLinejoin="round" />
        ))}
        {series.map((s, si) =>
          s.values.map((v, i) =>
            showValues && Number.isFinite(v) ? (
              <text key={`v${si}-${i}`} x={X(i)} y={Y(v) - 10} textAnchor="middle" className="dt-chart-value">
                {fv(v)}
              </text>
            ) : null
          )
        )}
        {series.map((s, si) =>
          s.values.map((v, i) =>
            Number.isFinite(v) ? (
              <circle
                key={`d${si}-${i}`}
                cx={X(i)}
                cy={Y(v)}
                r={i === n - 1 ? 4 : 2.6}
                fill={s.color}
                stroke="#fff"
                strokeWidth={i === n - 1 ? 1.6 : 1.1}
              />
            ) : null
          )
        )}
      </svg>
    </div>
  );
}
