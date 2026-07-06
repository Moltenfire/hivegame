alter table tournaments add column round_robin_pairs int not null default 0;

update tournaments
set round_robin_pairs = case mode
  when 'DoubleRoundRobin' then 1
  when 'QuadrupleRoundRobin' then 2
  when 'SextupleRoundRobin' then 3
  else 0
end;

update tournaments
set mode = 'RoundRobin'
where mode in ('DoubleRoundRobin', 'QuadrupleRoundRobin', 'SextupleRoundRobin');
