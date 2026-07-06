update tournaments
set mode = case
  when round_robin_pairs <= 1 then 'DoubleRoundRobin'
  when round_robin_pairs = 2 then 'QuadrupleRoundRobin'
  else 'SextupleRoundRobin'
end
where mode = 'RoundRobin';

alter table tournaments drop column round_robin_pairs;
