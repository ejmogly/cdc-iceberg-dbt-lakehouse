-- 손익 모델의 입력 pnl_calendar. 실행기가 만든 in_calendar를 그대로 넘깁니다.
select * from {{ source('e01', 'in_calendar') }}
