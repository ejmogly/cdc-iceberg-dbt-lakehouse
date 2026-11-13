-- 손익 모델의 입력 pnl_instruments. 실행기가 만든 in_instruments를 그대로 넘깁니다.
select * from {{ source('e01', 'in_instruments') }}
