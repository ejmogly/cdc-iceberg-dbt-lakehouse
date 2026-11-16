// 숫자 표시. API 값을 다시 반올림하지 않고 자릿점과 부호만 붙입니다(설계서 4절 「미리 예상하는 실패」: 화면과 SQL의
// 반올림이 달라 1원 어긋나는 일을 막음). places를 주면 모자란 소수 자리만 0으로 채우고, 넘치는 자리는 자르지 않습니다.

const group = (s) => s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
const MINUS = "−";

function plain(v) {
  const s = String(v);
  if (!/e/i.test(s)) return s;
  return v.toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 20 });
}

export function num(v, places = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  let s = typeof v === "number" ? plain(v) : String(v);
  const neg = s.startsWith("-");
  if (neg) s = s.slice(1);
  let [i, f = ""] = s.split(".");
  if (f.length < places) f = f.padEnd(places, "0");
  return (neg && Number(s) !== 0 ? MINUS : "") + group(i) + (f ? "." + f : "");
}

const plus = (v, signed) => (signed && v > 0 ? "+" : "");

export function krw(v, signed = false) {
  return v === null || v === undefined ? "–" : plus(v, signed) + num(v) + "원";
}

// 현지 통화 금액. 미국 종목은 달러(센트 두 자리까지 채움), 국내 종목은 원.
export function local(v, currency, signed = false) {
  if (v === null || v === undefined) return "–";
  if (currency === "KRW") return krw(v, signed);
  return (v < 0 ? MINUS : plus(v, signed)) + "$" + num(Math.abs(v), 2);
}

// 가격(체결가, 종가, 평균 단가). 단위 없이.
export function price(v, currency) {
  if (v === null || v === undefined) return "–";
  return currency === "KRW" ? num(v) : num(v, 2);
}

// 손익 SQL의 %는 소수 둘째 자리로 나옵니다. JSON에서 21.0이 21로 오므로 모자란 자리만 채웁니다.
export function pct(v, signed = true) {
  return v === null || v === undefined ? "–" : plus(v, signed) + num(v, 2) + "%";
}

export function tone(v) {
  if (v === null || v === undefined) return "na";
  return v > 0 ? "up" : v < 0 ? "down" : "zero";
}

// 축 눈금(사람이 고른 둥근 값이라 줄여 써도 API 값을 바꾸지 않음)
export function krwShort(v) {
  const a = Math.abs(v);
  const sign = v < 0 ? MINUS : "";
  if (a >= 1e8) return sign + trim(a / 1e8) + "억";
  if (a >= 1e4) return sign + trim(a / 1e4) + "만";
  return sign + group(String(a));
}

function trim(x) {
  return String(Number(x.toFixed(2)));
}

export function priceShort(v, currency) {
  if (currency === "KRW") return group(String(Math.round(v)));
  return "$" + (Math.abs(v) >= 100 ? group(String(Math.round(v))) : String(Number(v.toFixed(2))));
}
