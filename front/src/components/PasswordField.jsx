import { useId, useState } from "react";

// 표시/숨김 버튼이 달린 비밀번호 입력칸. 보이기 상태는 칸마다 따로다(visible/onToggle 을 넘기면 바깥에서 제어할 수도 있다).
// 나머지 속성(required, minLength, autoComplete 등)은 <input>에 그대로 전달한다.
export default function PasswordField({ label, value, onChange, visible, onToggle, hint, error, ...inputProps }) {
  const [own, setOwn] = useState(false);
  const shown = visible ?? own;
  const toggle = onToggle ?? (() => setOwn((v) => !v));
  const id = useId();
  return <div className="form-field password-field">
    <label htmlFor={id}>{label}</label>
    <div className="password-input">
      <input id={id} type={shown ? "text" : "password"} value={value} onChange={onChange} aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined} {...inputProps} />
      <button type="button" className="password-toggle" onClick={toggle} aria-pressed={shown} aria-label={shown ? "비밀번호 숨기기" : "비밀번호 표시"}>
        {shown ? "숨김" : "표시"}
      </button>
    </div>
    {error ? <small id={`${id}-error`} className="password-error" role="alert">{error}</small>
      : hint ? <small id={`${id}-hint`}>{hint}</small> : null}
  </div>;
}
