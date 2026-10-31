"""Conservative matching of Rust's five gene glyphs, independent of row text.

The reference masks come only from the anonymous WHYXGW detail fixture
(tests/fixtures/rust-detail-genes.png). XYHYXH and GGYXHH are holdout samples,
not training data. Controlled 100%, 125% and 150% sampling of that same
training strip covers ordinary raster variation. Each reference is a tight
24 x 32 grayscale ink mask;
matching never uses an inventory, expected gene sequence, or row vocabulary.
"""

from __future__ import annotations

import base64
from functools import lru_cache
import zlib


SIGNATURE_WIDTH = 24
SIGNATURE_HEIGHT = 32
SIGNATURE_PIXELS = SIGNATURE_WIDTH * SIGNATURE_HEIGHT
_MAX_DISTANCE = 0.16
_MIN_CLASS_MARGIN = 0.07
_TEMPLATE_LABELS = 'WHYXGWWHYXGWWHYXGW'
# Losslessly compressed grayscale masks in _TEMPLATE_LABELS order.
_TEMPLATE_DATA = (
    'c-qC9cTiMIwC?Xga-5v=Fw6`v3_0hVbIw7DB2kbiNRS|cBA_Tai6BZ+lBlQ%1`x!A5ez7b89_kj?J=EG@19rp-g>X<^<UJlsNTKT'
    'TK)C6de79AzK)@5PuJhxAM5G8^61_A+}P>n?(v&9#!j^lT$x{6x;NU@J@(-3+LO`V#;&pHuI9p|!s?5QGrfm)W*ixq{V;ksCn~48'
    'w=CD++$X;I-e5_jjcxS7%dgs^oVDC@52b}`;gxL)#~Y(eh-8Z$J&#MxrNtG)cjd<$5*19dM~(#RU~z`qS|;-h84_x-g=IU<DQvye'
    'fl_Z(BuX==^88L6DUfz@S$%<>BuhQMv%rZBg(<ohb#GUfgc|Is?yPW=l2M3g%&?XQfn;sd>%x_2X!HEWbB(?XhIGKe1QVK=7|l4k'
    '#9y9Hv@1S#qbpcOO46+$LYFKeOwb6-aAh&1T+2J>&qv8g;;fSW)QKX(7zMXDTc)(E&ymxgZ|qQ@BJ{(Z6>*|MaE8?uQz;pxEe*q*'
    '`x&ZKh?=(r3o9WCr5L#DP+6LhZKIqAIodQ3+g6W(gF)a#b!%0UykSDm1m|Ia5nY^aphCf-Q3$NO9t*2zwR7k;=W(&6j2Ky2hJdG1'
    'uo#9C4XNyycj+-_uFP3p0?VMFNpfroj!MUYmA&>)zT(Uu_ErTWaA<_2mX0(YkAsP-1|Giqk@KuRSQ`q3!r;>8mWo6aL`+CMqVe%6'
    '=S6FjAp$H4Mk=~`Xi^X&LIPUxZBN%ZFHa_0pe6XkF&aU^hLTWTK0dwF?uGB1#l9SSJcvhvXdJuUiUH-}6Ew-`U*vF>&h2w2fq5Ww'
    'yItANvTz<jajQMUuQ{A|V^#i=P#(CP*Zxu;dE_P$sAK7cB@XA~&Gt|_j0de2Qd=9Wis6;OxL00&%i%0P8jO`e^5FGiJC4Wb5cy#g'
    'zuK$sIGoj&S5o9qo5*Hqg9FJ%R3S7oxMlJKhqLzncD@cBEbm@4cDcYs87-}~z3cWz4rgO!zT8HQY!Fd>Yo;>POjgG>`^>{-4(I##'
    'r6yl<MW?jmb92X%J#_5?4varr;Q-@pv!8{M^Uf1b=i5`g^c{lAt~_7ioH;Yt-#2{o<u}fc@on+Wg`vUz{{F$i{?n(1re17(|1maR'
    '-5MF_KYjXiZf;g)`tIh7Pv3l6-&pzh`ud5IoUC1$nU0QjR_0Er4Oj2KT3LI)aK9_x-`>{R%1Tm_ghNTW<(|I&<n!w5hu05>s!~XJ'
    '9Pa-oN79@!PhPtF=H2|v#RHp_NW`BUiy&LX96COHV|ui|Ez?bo0APR<F)*B7Kz3PU&#7b8`?gszaJV0wfC3{FtV6c%+FO#F7VD}-'
    'M4~WUj)#kh6BP`toIJf;?95bYFc1vI<v55SuP6*hqSEP-WCBt`P#A>x!NCHX1jS(p6b6e%!a*W@f)YPDR9Fy7kY+2XsVgh6C`eIJ'
    '2=dqbVg$CnnT?aPy_JcIBvj((`~)Nj#xnH_k4sD65ff&oii081Kh}?i;`9P@4%DCQK2}>6V@=0kus`1yNwrTqHazuo;r`^geF2Ij'
    '!hed}<H;jo{}#{xDStHa@BF{cm%KcSNmB|a9-e--{B>z=x<1xGPL|1F=<Dlfs2Fam9KE{$)Z2@D?dkTq+8XNWc6Qe0CJrf$6OUf6'
    'u77$xcdEeG#>(8x%--I{!qhRfY4QhuGk?0!&(_+)-2B$9vB49AV>9#bzXRpJbbtJ8Z`<(ni-nsvNBfWWk513M`v#Q%n|qhfbhi%O'
    'c=o)rvnqdk*1npdyPsFrzu!Myx;r&y&(VvQ_U?^y*R}D9tT_Mr<LdYE1AdMs8r~U=P4V%L^5O)xUPAl);>Weq@$yuJkg{Llp7Qbt'
    '0|bH24z8NH``~f?76u6oF^p|!?CZ;RAP|T|x4omImo65$ag*YZJ9Osu?ZW|d5*lieak}gH(H$0O0+kU^d2{CZ^JCGf46L|bOvB;Q'
    '!XPaeML{2U*3-|Q`*)fs;DuEKi?frq*|I?lefPrQ4}fr7F7eQ%3duPqNBTQy)5O{KF|`vL!1kYNOmtQR$(RMXS?aP$V9lUCU3a+r'
    'K|gn?bPX$gWd<1oGf8N<IQI<*){E;syX*;4a;mZv6dH?j%p0D0#f{<nx#>dzOu7^e4;M#EvbR*;c=C?RzkYsqGFF*MfQbw7qUDTs'
    'w9ma>{{B;S&S#k@5=8}h{{ye(Q8={p>FX~(Ug~4W5Etg>q1(h&k8=b5lTS5lcT$iL5!j^QSJZW9edF6Ney_V|vlc{LSU@wf?%d;F'
    '5Eye8nzoxFpdc~hl&)*f|K_i5p2@YxAmLD(-6OXb|K@M*jTX7!Ferq>p0U|C|KPJ@d$}CxSTufr>2JPt|4NDLzwrlGxg3ph+I!`}'
    '{{?^d_{Kqh1`tx;ieJI}IX*nQQy;0Wrm7NAclY_nzxn4^kJ^&VOifI7v^`$@{7?S4J=xsM)HJF6$uEBY{+0ftZ5^Fge}o0dX~)s}'
    '?n}4t%+3NnsI~pX_)~8EKfBu1-dx*x;nwZ*=Ua;6w(m%;xv=p1-KXo#8A-7b+0}jhEiKuB`UXZO@%2;p=3ez?+L-95x+NVx0@S%a'
    '9F9QP<_uk)x?kl-MuUYEy>oXD4efR!V&EWyxTdzw(^+<0?K6(6Z<w2F43Q?ngj53dmh3+et_MS6iO%`MXE~fRDH?PHpR|3#_V_pl'
    'c?qDjLaJ}y;c!Oxx@b{^Xa+uB9&WlcF|w*<+R3NSIGh`GvG#00iH!_&wPmm(O!MFaBkzF&b+&h3hz<-#W63aRfO}B#+I4JtZ5?>`'
    'iBnlWwjLfxJRD{iT{HY}eS^dKwD6!N2-tcM2~n<MJLC>dEpQ8NedTj^9AH<31$h4w$1(yAP5n&G#@gyoCU@%r9LCU!X}-VkiOau#'
    '+qhWdsv`-M?SEjDZ8N)Hy#4YkHDmic^=Mp<RddQY_4577FMer{qb7wr4_eVO?Zo_>-}_}G*HW3t<p^1W?MENH{4xL9^82179Tr|p'
    'Sbz^EsT5Lq{n1-4|M2v7)fSc{mb-okhT@#tcVmIeUrrD0cEkg23=RPS!yvX{<O!EQI&);30SrT?(<s18i>mnL9iLuV<#48(lU)_X'
    '337nUsxmO5GFA~4BTImxxm@g~LlvTEIXl=|sggy>YBm{N&z>(WbtfChVFj61ky|6etymH`X|;&z>6>@%R0qqE;X+D2yYmYQH*3QH'
    'A#*DjK70OrffEicA*8jfuBr8Ssudd8u0{#1$2vN8I#9_(ynWWGp^>oz{xZP1YaCrwedI`@Ba5w|6nyaN&HMB133{q5N%P2k`wI)B'
    'oYZxVEmJ$5EPmR!xW~y%OWryxC%2#=%2`9t)H?knH;h2dSm-F&0Q^ThKR&;A{d{kC=ZWq<K;u`}Hh$#m`*&b}T|Eon<DI7lCg#6<'
    'UFYU~^~2&+f8BxIDM^V5$?3Zec3ix($mKwsyNb8^+8Y_@>lvC_1}5)6b&so`uV&7chG?-!fH{T2kw7K`4~(vU<<?<Iq$L9gGL<e('
    'BVy>v20;5T%i&xp4>Xs9<0NI3wT%E}#}SB5y9b9hHU?8P8AyH%+sM`<$X1<-0Sf}Ls{inzW2-a~E~4O;mbd>%Mi5{);CkU@<u6{;'
    '`(h9f3A3H2N3PxO+8gemK{gI4D7<&C%uxaafx1_5;{dE7&P~fTwrKD8c!9Mz6on6LUi`57{pHj^!=CN=)m`0ved#8mZ~{Hz#HW?@'
    '?_ZXlPoL`;9KUn7y*<G|6iH#l^sKI~d|Ced>G|EOw;sP*Y;KO#6Gc+lai_k1cs2KM_RhrU@c7K*C$+WPbVN}!h3$RoukT;IbZ+o;'
    'Pv@EOTeqvLBe`4=;7=|ctFPQ&QoQ#_TmQiC|BpYtHr0|A6YS;g;<RN)L1BG;wC*oHl5cOI$h8e}Cf+`+t?~Ll*Rygi-$7Rf4HEnh'
    'l!Aq2Pfx0e7<c_0AIGZWyo_WCARaWp&z{XP7lUHRTUwWHbd~M!(vv}M!YEo;-ndcX00Eqnd-<)=(?|2et(365Sb#r&Uh9p5L&0Wg'
    '-N4u%>!?oP<MOq&lOY6zgrMr?{dFhK97*xiA^+eU&QOvn9W5el8Mb40d1|1!5<!Tp;ou19RgtqU145S9vvl#Z(`8Xm;tV4nUk+!o'
    'HpWQ>g+w55R3-((troU*SU9jBhpSUP)!9-65>*N?j*`;yj`8IcfSrGMy(QDzMwczeVzK1pG)*0o>&C`^=*)C$Mue+@wwjWfw!Y)$'
    '?e!B}9RtR#_N+)ZLmhQxbsYnzEs2d&-#H`0CmT*r%zxN;I^N%YeB#~8#@p*BT57A$4RzG@UVHL>V}9)Pv9@bVUpC%eJKh9ryRwXk'
    '?8Xc8YuBoCwkI}BK3#s=o)i-nvLnh-#U-VAX7yCOlfF^%$*FgD_n7J^D|*_=2uQmYUj9;VD+Pu-?H^va9)*(N=MD8z6qK^bJpbv4'
    '2~iwsm38vrrBJB2uuwv%h7i>_aqvUAHcEsmN;iiC#etEtU0;N#71{HqSQ#uJrXF-)wA)KW0s^bZF&D)u2R1**krU$=k$27RZg3KY'
    'VhOEf4q_--w?o&Hq=YsJG0fxZN-c$ulFXrIPYDFgx?nJp#Lpu@*4|v4Z77J9Q@%M641p01l9~ev{JerV1-FbuZ6UI{{;P=?I0UU4'
    'y3Yl}&nJkKvW$&T6{Hzjt=>;YgP}6+X%+|pAyF7!H^^6BNXEu}?a@vW3`Dhx(1(dY5D0{_i=(uVf=BSTxokQ@45Q<z0v5-Rkm5{p'
    'Gn%m4<~Yu?0(p!mRKY@40wyJg6DDbD5`=VPvp6qGG)ZFOl4?{CPR)QOgp!p-2%2Ob;4GAzGC(3|8b*R@>dY2`kcc2b>*7Yvt11V1'
    'h#(lyO}1OGy091=E+XVyeTuW#;G>2R5RrgmwL%gNMTNvf1x0*1#yGE!h3cXB_<;V?JSpEwjGvE>Pb_$Nmb28AXo2J9;X%r}6&`j0'
    '@$+mFfJTkK;Jh2iaU${Y@Zi-#YEA|~0WN|~xUt0faN&Rtm5*l=)hMCs!dAE-4<A@^=e=bP;0?kU{F^pO+vl9Sk$@NF6+p`6JXqy?'
    'xjT@+64=Bm@3a5v-JLWrzc4{<&*L@D>iqRA1%RuC*W8}ZV<82_XojUvH#lo=9`4tNfgtATCmy{h)n_1as?Jp}zHv5|mm2IPNffWr'
    'v(H}DxM(w27GbS`hXZ80CqP9(DYE7I;+tb(_L{ohsXaeETyLPNqH<L0<m)$W5e`~<KIx~HIHO$$^0F%jp57U%F373qeeiLtF?(lr'
    '^U%ogJv)>4bv>HuF3U(RI(~cU{Nd#7DK)2i4@G&qM3r9~IvD5S9A0w%QEQ5egJ)jz(Od`Mph@m;PIFXZnZ_Q!vCmtcr5;^&Jl|1`'
    'X&80laI^`XqPe+dB*&3KSB*Jzy3|vPp&Qy#8lp?VDS4IlrdUfcG?VJj9}du^YXsC5`l;cNvi7+xv1T+Gy`8Psn!*g}Djo+i-4xMK'
    'DbpPXLk*?aCOIeWoQN@%l6NRbv}3_RB%Q7KzB*Eh)`h+E11VNgGUjPp&6!X!tcqWXt0qIuao@n(OS$$^Qo7LrhBUAUlI6V3R*k9U'
    'QE~3eM6rt$StZa-ivkjX(JTT?l%)0jt47ym4tmn@EEj8K5?B;U(s$LBmo*7(y8eB(&Yy{;Ss1YJU{Nqu%~F-cvW#xK$$8ik%)*d#'
    ')fsp=1d5W^XG_c5@93W8JUOvd0fSYNqu|j<B$A;*mr``e7@XtG_r|JX5lk8$a9%hBMVd%a_R1f5$$2)AsD+0TaYzzfS`vjPAqmR<'
    '`>%fByckY5AcEj9D21)c!lHm4LpAvD%va9Bg=`Bd;A5Z+4FeTCLP8j&5!rNaowGPzXfGupEC!V`veqF%h4_)$aUBo8ao$Xpy32t0'
    'MG&gC?xs`-A1_KjsdpZb)tO3vHi%agqwBHRjt=GJ#hC0I0_^1b+4?YL$R<&OS#XSp43vi#XPGmy0NBI1wpexOCJ9N0*t9@-_$EP$'
    'ec|O-9M1AWcZv>T6Ij}7XTergG_NS#rR3ToU?1KN<rtxO;R;&{%M*0)0w7uM^2s-Vo&0inp9O{wtr1mTmtjH?fh%o3GV_)z=Tk@Q'
    'iGoC<<oe@<w$fm{*4Bo5@45V5yFVKx?^1Abu+m?f%C=5E{^%puw#*Mk=`!>~Du>5fV{KIod<y!Wedd<`^K<b=ER(G@7p@&oaMCgh'
    '+&lPUnNwZ4Cq4gQ*X@-xz^VXyv%YXw-oCy^i)AId;&v9dPJUeZ_I=~ybl0KG9qGGI+<6wi&D+8~qVV+G+x7L8muJg&c-gxJ?mIi-'
    '2zWmQvxpkNK7D=v;8>EY3PWBe_GrJg2^%e~AAI=w)791Y_gmua<;YCc$cA1U(|_l-X7c}!I2*;#_A5Di=k3yy@yc*ZX(HnnN7B^X'
    'vO30}J()bUC%}k~r~lxlatN}5b!<iF)yq9qNzPhS+%L|8;pp0)JNDEZ-ka)Y$i|^*Ke;RfftNM%433Kq^)gkUA`p^4xwM1?1S_Ly'
    'XlY@j#Udglpp+lngefK@0Gv!j3Ymz3iV6#Y$iKNDpMV4sjl-f5AYnlP3G(lG#KhrbmZG|v5?~S_q7cgO=R%PbWfL1`CtFihDI5e&'
    '{k={!Ma3a<XGu|JjGML$21Wb*UL-m5sIty$mwN!utV#LxUO*fqRb8@9Twi=~v%l!?H~?{@Yxx$Rz5C())2oO75w|~a{`%p;(SM8o'
    '|CqnO|2MZYSHQ>^gdCapokuoBT5Vh7DMu@1yqs}(HBeUoKbqjAKx1h}xAghA8pvupY;U>!^6T2?xy}qfJw;8^loRK|g6y;`eKSrz'
    'e7&}@vM{h`tBrw$bMEQOks(g{c0t+QkC)aqzAm0AiFPotanB#P(%Dv#l~Zu2bNtB)uxDp_s`E1Q_H|sj+1^x~l(KVg%emRl8{a?Q'
    'XgiReke=6kapF)(oU5yQXm<U@7t3q!&lE(t+Ixo=cc0BoaaUGQFbvE-ac^k_I9n~*ObzSA`jbbBgVb1bWyjd+D-Y&p%H6c3XqpZg'
    '9cKpHb{H$sWX(1g_g}u;mtmnIC2izeGBSN<w8%+|&d_tqXzl8#iqw{;E7)zTzxDjp-RjL|EQY2{OxckG+1|=*IbFZZF2J|F?o4o2'
    'VyYMgW#y(uSW2^$Ew>#w|MnZ_{h7kedTbSK*XZait~yjXU5|{G>z{yLXRJEGMomT8!pF<bfQ?r$4=WwKyZqzb^L-4|*g94~$0P+D'
    'vPq3VjsU%yyIK=%r!AwTsUib3V!DC(y+8~By?Q&}o8@D`U@)l|IF4c(ReAaS7eGURbNNEqRtqN3r-493IhUQSQ(w5+@_qg5bW5tE'
    'G!ZH)AWYE=Dn5Px_r1--(?!0DBnbf?A*Mxq-RQI5*A-7MRYmGCAws-jD&Bd$w_pF}i_;xxHVOz4ez1Pj;j!l*|6G$i94hqE!itL^'
    'EmDroyj%Hm?eX$TWrPV4EP->%@10v+|IJt4&opkgrotg)kNsyBzWuo;`TFVJu@rkLB!cE!aqiXkKYU~Lapx{qCI-z2th)4PHUAwR'
    '^zWRxxq9@?Kls-tU0H6@Sj_*<*XDb2JXi$0Y;f)6fADYX&j(AkXt888V_UDi{hNRPzBp1DW3HuPp4K(}p7W3K`eIeQwVtj`X79`|'
    '-qBnT6&06NH+<{$+gEqncE)dw&8_U2xYJsj6}%-hsp8bc^Oy5C>f^%#Leol5T%9>s6l$oa@3{S7_r%nd-b@=qEfc?#s&iL&Cs`BG'
    'C>f{ZLw$qYhyA56FtTCLo}TkZ_PCJ|Afj<-c1>MHrVSbbqL^<xI5IhSG+Y)df!6Sd%FRs;)rCS3EcdMTTk|vL(zR$%guI2<)`$QH'
    'wm6KS6;?6){KMSjK4&!oMn>Dh)!9myCW4W(OFeq!&C0t+O<OIc2~u)udO9ky7$LG=P;vjA&)=69`U``!q-ZoIOGb)>5M?<g)nA!g'
    ';jF)(XiK${qv7#*KyRU7^)1D{vx`7IuRa+&;H^eS!oU*Z5>TXZV*TjChxH$Iek{^Z770kRkQj>Ml;3mf(<--~H`dPrRfrVZ^cSxP'
    'PA;(W($nwUQu+Sv+oj#^8d!-<JbdCbt!<6h7k(w>`;~n`24pBNj{ro@GNof?@po3Pm2NSjKzRY9EpL<FdFxN5jPLc+C4zzf2g{fw'
    '9GiOed!Er>^9z9JI#G?+Ui^6O#>bak304YdkO04+IG*LRfAHS&S74oA7iMa<=rDn!MMwx}<ZUwAuD|)b!P)q9XCTi>k%mIRARq}a'
    'RCn8<Vc<;XtN<kvZOEVyNE8wl2@zB9%5I;2@s;yozAMvHkwl@%D9ABLFd-@PsLG2EzkHvcJ`!vqg`+EIn;PpV5`>8w-n%;|msW0`'
    '&$U*-qGk1LygVF?=pra-^Z1&P7az_Zk5#9_&`LG|@$umvDj+yfBdDln_C@PKe>z$MtK*wcT#_4OjDW(W9g`cc&z9vl;Gq&E^KFGk'
    '+v*CP2`~`JB%=J>L_wN^6q&5zlihIs(opkOMJa-`McBT+OX=~B@@yrGn970K$9K+Wo2kpG*aR1z97>6GQdU;C+fjFZ?#1KrLK_`L'
    'b=%;AjsecMFYg|nt=pHAoERUsJs~}JU-Qt^i*+Eq9~P#2%5syVwuXm=N5rP&RGu7tw7j+f)Ja=Gn3uh&v7w=nk)?xIbWZKav)Aj('
    'lgDxbbbvOU`_%|XrpudnCO6-_zwo4ccc8Hpk$@*rsALk6NS4#`$UA**@^V=aplxu16iZoEQBI10#1hrRDjWJvq?k$~1mFx!GiNt@'
    'OHC#Q4x%|{?P;ot(7-}Pq|C!oOAh3x1zRc6anepHS^EpTWT9XQwb1g;YttiL`y#D0=xm3$<ZK{5U?{>kwd3;R7Z0Yo_r>~I+ehr)'
    'vm?fW3_{}Vi!R<>TK=#!bE+~oE<V4eVOyvX0feP_RZc(q!j;&m{+6->O}%{~0eS=oLE69O_6sh5_u$IViH^ass{!8Hcqo~@rSbkN'
    'uFfvMdN@5cHvRCikGm!wMpX!Dnw?vCIsahh>V=_GeHSLDJe<|>2uY>T=GnWqZ(JJaZf|O+Yw12a;_jr5N79tTT4pCM^fv6z&q_;6'
    '&Dvkr=;f+OK+u%JTJDY<FH76(Zf|a8V&fm1;Nz}^hXeCB-#%Tj!(CmPh=797Y!iz>A6){BBpX;a-Cvs%Y_7@xJQkFxZyfBeM}QKT'
    'zEzV$$M$b`H<Bkn1jMMCx=~@qBnXD$vj0L~$C1nsdrcb9B$L&(ljE$YU<A%`SLex&hN1*t6QC#O@|<)>Af9l&*qWyHma^<{I~B61'
    'C`DbnqS#*n1p_O2XYQ#e&rjWArNr&pwe*hGZqp{fMQNs+<96kw#rv5lkR-r#UBffIyX@IS3B01IlmBKv4@-4A4vb`*S>L=`8D=bp'
    'M3d-jHBB{T844N!BWgIgFFfwb_BD{iz`-yS8pXYVLgHjCwnThfn&>DBH&bUxN>aIlMpw|Xk10L0@%821!P0OS6E#ISSr$uCP1o8Z'
    '?$Ei*oNw=+jaSF}TI;AOu;t}dwT+#E(wnZ&R20Ogmk&ICK6gAPKE2`c;+uu`?OS82PM7Qoi!bV)oqN!f5*B~>+_Q!G#_)iU@~(u<'
    '#`Y11#-^|5IUCpl1%CHto}q@(&UznHqKxIvQzK_0<S-1+qVr=Hw_+irko^H>SgLM(dw-8FNgQvL-Zj`C020B4AB?molGV00c6Yd<'
    'M9_w@O(#xxiHVY88&bTeB!$hDtxdKNLAb`2iuwj;5pjBQdqFURB<r=es?toH53J~wS6XHx43*31JQO2KpgZo`SF9(rNnF})NA_-0'
    '5tQ2QQ*ApHi4@E2*;yI_Jfe~&TN6|CMeuq>gTqDYM7%**VuBJMk1#>oKRilHlxkjjZn9F3gi`ko4`=c5ilCL8d^f9zF&!!|J#I0j'
    'z~$|{eHr`$Vn|tYCr<@&wrAbd_q{e$2;I=mfhr&(0i)@d+sR6(ZaI46%b+tIOi?p3Aqqi}aFUXaF&(56b>h~>c`sQoj-{rF6~z&e'
    'IB6wKD#SSX)GTK-NCASP%Cb=)Dd6O%Fxdo{b<Xe;&Um;w0*)a|B9JN?R4kS#iH5n9US8l_i`BzG!3YeTU}PqPgTYZyxNq&$JI-W^'
    'DFG@9MxdFFuF3>J(m|+@<Fm`0>1;bHR8R~`P!8OpLlzSj7RSa6Jl){jDsrR4_(Y(R`myn*G!X#-2~zSU;E1_b;m3k+5{Ajx?8>rZ'
    '3iI)Sq;^d%aX1fZLlu!cLU1L|(*5o%VP0OSY~dZiM9j6vX<&Io(AuF5^?^#Fe0(V7{SN?3_Pj4m7tbSxH%;j2jMR`25F}_HoCEyl'
    ';-!33@+J_~A$w#vQ6DNIB56|d9PpoSr^{`nc%e+MeUn!*EifREtX<OrAUU6&G`PtMqU1wr@6QxEQjiE`kG4fXa#okRLv>J0gXEL*'
    '_bLLFm~_*K-nW2X{kC>thc#W@Vb8f&Pa0!QG}YYF&VE|^@s%abK~~3Y|E1S+EeV!-+CIB3d|u}a9y?rn^77*1{qx6;G!NZbes{08'
    'y0otET3bbSUS-dNhu4mm?%LZjy7+9oHaEGj<!oWJi^sO&3l}?bH{1KBHQl;?EZ)W3FQ+-hQv+y6PIevycCn7%o{Iwq+*Ft*ag`~q'
    '@@%7!#-@CCB?8MXwWlrDf<e%YKAhpkVru$SR;F9gp%kNSO;t%i{b|S6?(tV-sydgH#2b+%@S1@YB~hA0tYOm8gJG&n1?%0rw`$`='
    'QA%!kS;4AAqIt%N<~S{etWj!ekSbaTDr23L;G;;U+2;24r0FvlTG7!S3J3uSszF4U6PqgIQZ#rm&xA=+3fgQh3lk8*tNZy{$w<n3'
    'm!6;AZzV&KadS7LL->VJY$qophLlQR<=B&I2U!Bu(pr}S5fp;a%`CK~7+T?V6K`7F<Zw7$0~Nr}i$cjdy2_H$2C=QTKA-SY#3Gec'
    'S$H@Y1jQ*U%aT|Y$(;|@P6eytAkwl@7!(=-N6W}a;@I|C{m;G)Mrsp4WGWGbrxGzJDjA28ciS`ahI207kSvbIppaBHlZeEk;czA2'
    '@~fXY7gNn8#lS!hETf|$g+hSEA*#VOGwYnO9DAm?h$sl9WTDT(Ktu#V8rzP}ayZwE+*lF<LLj`3o0Sq)jGq^zlhE~;!<nw|Qv~q|'
    'fF#YgxM|@<Hf;hKqz?eC$DO)RH87t5Ox7iSvmsGr6EDmx=R9EH9<;^i!1#nv%7NL@mQ)d50hG<2QDDE!^(Gr3_(kwK+xDkA(#80N'
    '3C^Wg0SmWqKF1QxFGe*>sm=3}1qq2udLH8Lqqo<K?Qw!2hW+m22ev4~#30fE)zj~QCS~@pCshKa=yPD8E=G@t#H&O!-v;*A`pec('
    '1uRW}Ys1)y3|khBWs=m%-Cvv!{i&u3TCSPhx6hVtHr6)sDmXL0%<Xor9tg7Yh^riZK6NZ3$U88x_Ua<AFF0Rj>SBGvcD3AiGkdBy'
    'E;2T^ZTjt3&eX+YM=I-2-FWxITYh_UrL+D>ZRgbd`Of{BJM(LX9xrn}@TUj8hw^vj9vYsiFAB0YcZn$*;XZ45Zn!Ai)y5(G;K||y'
    '6B)X)bK1!}AAqIZYTs_7Dl2cDaX4?A1|BVC7JHQ2h`gI>iZEajDEf(|IpNAkDA{mZ6ZgsQChLN=sVJOgZ1Mj?j(`yLLaQ!6|NQCc'
    '#X|ua<UbsYQ}-?EnST7>+R1!ZB|wdTaS&R;A^C9cxuK5o?UpiFIQA!pi$UoI0Vzdg`|}cgG${z^FAftH#juQB0z$U<I_gUyBtV#-'
    '94f#o45P>@swe@K3l<U-L;vIu{=YT}K`{gp5r+Z`Y~m9|{r-O*elavzib0nop+x!l|8PNmQ6ybSOHW5bi4GSL{LNv)B1opbqwnTG'
    'FMC}YTucJ}dz}zT9iJV0%1ZMRywpiB2<FdwDY<4gU%oYUt})$?_2<11V1oAMijjrY&-3TYd{s%vf5a`k=IWb`wZ$vdL7J3*iSvzy'
    'kbjN;|A|l5hx`}$%8OLTp@2GRnfftLW4Jy8Pu5G=Uz}h>r^-8|blm<?S8cJDN=%mJ&dS5tF52oQn+pdX0%yvn2d8)WnCTjN7qzri'
    'M0>h#POrWC5;!AP7RD-*f_?oH>-&4^(<8#-_q5+!`U;#KZ>E~_61GL>bX<5cGu(FgaO=?Y4|~gbeWA7LNMrBy*;|*}N;7vBH=Mcs'
    'cKzG>^8NPWtjxli!7FFmbGF!6c*N{G_4v!i^3%a|4{K}BxQb(gjfwX13{~5hBa?4FzMQD_QI%%t`R3PLK9S+f!qYT;@`vxw-8!9T'
    'Aw{5R`<1lLj_eDNN8?yFJ36jh8?K4d#G+{i;WYyvXIr;x6ENJr`+A#p`^lkja&{@*H#m#u3azCuBz5n-jYkTjY-vcUmjB+-r=0gQ'
    '4L%A)g1mj&-mF+pT|7$8I<5`~)|Y2}v4#w?tZ{f^gr})I3ajOp*FC?+S$luAz+H_Yt?3@%WTGO4Ae+ZlU0VFc*;u((A7d^rtzc@Y'
    't4JpxnC>}U+?apgcrjSyr76QyR%Vj1XpC}L<>jBT0<sZp#-`H9fHtG4M#*h=KmIspfhXj-DihHVQBep%-eu3=v$bEpE_`%2SceQ1'
    ';uD0?4Z~}$Ed9CYIkv+}8YZww5T)pn*)#j+LT90`z)J<m&m#;JWX0u$KNmW0E>~?ezzOn-)2-7x?l1qj(D`tyBh8j7DhOqJl$?FB'
    '{_QvC>Xer>NDQOCx$5ehzb|@LUX35z%!Y!AdRtp={^O$L>(WG3m?9iXHi&BH>e(OuZmM>x5&}jsiaqi7D!FewUB~6r|HN<BM=B%W'
    'lE(i{{^3?rj5-#>v`FZ@|LJeO{&}`7$xw=>?6kA*$>)Du46QslmFJ?RX&zjB@$cwxbM|VqCf3U{B(rJe{n|e-fZk0W%iJ1~Sls#G'
    'SJZA?I$2jyTHbQ$F);7@>At%Ch5K8EZrs0ky1H;@a%yqcjhD+S3l|y-l46oe+b>SFA5QkSHM8(9>bfz%c&jnh+f2_gys-Vuv9hgJ'
    '$_$2H^ufMsGsF2l+DxjVdwNs<xt8rVYyyhqomJC$x<1BIj)+(A&OLGQ?ohU?5)MqZj>;>q-0iJJ!xGd&51g6)ICCgOmkh@0dq=0F'
    'gqzD#h)lEOqm$1#Pur7iW#K4gOV0p52Q@mGuHm(}|Ir7|%faG6Z30SK!^q4;i%FJLvWu%9f3?PWd#x$io{5o^QBVYAn8?)MvZt4u'
    '%TEu6N`uviWGa;c=rB>iKBZyoDR7ppzMeXkVk||%q2Le*9N1j@2JQjpBWGjv+4&Mj78wZ=5fp(l&0?A-U#<T5oc?aAK0u9z6yxU+'
    'L@79DoPPK#V;^Tv#F)xq#rb(e$htwrBhSD5x`uqzyTe9_Ai>WgN;3{GAA7a(hbLMo{@{{Ep$A5PXY$89Y`g^jCQ*uBV9~kx&p)ps'
    '-%T|H>q=w91$adX>K<9AX5Vvvuk!8d^N~__B}o)WSU?oRw%LAk;^oQ)x0Y|Vr<u};D5!*(IE<<jcwlJu-B<2Y&z6U(5eRthcLx|w'
    '-XXo^3U{UNCR)>-Wzb~0j4YFi#WM{<i~H_<<a~d5W`BqQ3B^#+(brL?kt9`|b~IgGT>CcPp6V!%LMa$Ky1AGuNRpU3ekB7B-@m_k'
    'FhpMxg3)pfjfn{`kR_31EmMz9KD|4X<)Vm{Aee_`?k!1oQKMoA>S5()Z;c$?ZY_s}Gu_h;9XokA!bBQ}Vf*BFjh#3YX{kt~YDScH'
    'UYh93^3aeX$$MlTJ=0Vg<z%3)>6qC%_Tbs%kytxTMP0vLO??ezDIuP&p0QQuAHV-R*Sj~;-PSd_u(iLwA~QC0OT@0W31H2yN2=38'
    'eS?zrwGROCTwS~~+*VUjy1%r%vUy<Q*{AiNm$a{M4>y)%r6eXKCZ`uv_f9<h{Nv|8i_<5HqCBh&_4V`(O&vqC>MtyCmFB~(uKZwg'
    'Wu~O0Buz?+ZRnNKFwKqg!l}Y7rfdq4NTt!Jc%qE1=MRNjzEvJ<!XjYE40&Z`772@&*57jQ;=<SY;RIu83<9rUVC(E?ra(sHWNk7!'
    '?!B8h>Me@`BW3Nv({i$6>{O@(DTC;mi5CN9mIN?V(k!)c_{!+<G<RKjd86?1OOKA`=pw`sa-N5;Jpb_aR#Upaouyl1)s?wJJ5-?('
    'SoP53_usF5eR;XHC}n$ES<n4<dm`n*AfkTU09V_W=dO0uAF1oU`f7F87FiHP(kx}<%_`RiKA#>vcX8^;$Bkq^CJ4r`${1bx{N>a8'
    'H_vWQT%DW+DtEgVT>>s`lL@p`&mZ2JxO{f#%!Nrnv*J9YKu8(eU1KjEObmCn)E+rpbM(~Jg)e{jld%(JyAq?fZjDYUYQ6C+-jfbO'
    'aOaurFOT!GHPO>Eb_mJun@aRyfZz<PotJL4XL;(dNGJq`X&7F2CM`f31f`j!oS$yX_R^*!gn9YlY|pZuoKO}BOfgCrnm&Cf!CjY$'
    '6czr#_r)keKzOZ)j`4w}{78FMB3Kk55AeF3T5xfsyw}0r?xSU?-g-2+1d7YM_L||rP?|-0RZ~O7t^gwj5^#%N<vo{bU8LX;yn0YZ'
    '@qv<bUqc28f>ri8c<NqHm^uZCW|(?!jgJqpQKKSZB+boLgYT~8IH^!j7`l><k&(762?0USO`@7FtUqc>a8j4V;)s%T1`UrwV5PLY'
    'b52ivdo$jc;-Sl;kttLvnMk6^8~S7(omv3S_wj}de+w-o1vZ;4ucBwSWmoH!7t5TDFVDx@3Zng8>}_mp?cKK|?rp#M`iIzmel${('
    '6BiQT<Lwg=zN4^V<P}iQQ$zK|m0gpItB;3kO6t!)TVA=_RkHg??|8@F#H^~b^Pg|F?M^K0xczSVdUfKqyypJWEv8Pf)l*ADxo$?@'
    'c|%X%_a+)^`R8@z*fEt|cVBqkwpE3!v$g5=%LZ>roJDePt~H5nmel{K)<+V|@+!T2zsv>>GEN>Ww8fFMBTwF`a6k!CY_s|%3yj4@'
    'jngldd5{RILCurJ7GOTSal*0l8M<O1tK93gn<eq`o`)~w7>aB{X@ypHC1{Aj9gFU?M>BCU4#mAmnu3496g~56!&N|dkJ9=6G&!7<'
    'S!UBVWxl__GPX(k{1qUyfXcUHdsKi2#~cWf<NHfo(lBC|D+?wYR<|-!sfEWX1@3ZV@@*0$sQJX%NF$YFS~eaZH6$RVonx#e`2m;C'
    'vJW<)V|0>^a~66mNHDTlpdm>>6ojN1d+5*zrn`DLZ-yPIV6>)_I$ju#K;SiORH#&kJwu##mpmC@7~7bQ1(As;q@2DiiSAu-iSyw~'
    '5E}-PRFy&_<z?_NNks}?I;4Jr^Jy|d6)BEo;L$`qbt(i)Lu1%+$M0~KZ^r9jMIl%uhGB0i1A?HzDCLa)C!8<0Q%#5>B48*%Ilx&-'
    'LKGx|)F`_2inB7CV?!0;7lue0#0F}D1cdmJdX-ZjIjfI~U6~@A_+hek+0jN2K3-n5Y18Z~XYFaFKU<WC52fl`kzozv{fiH8(=oTf'
    'Szl-hR}tgk!|O-37P}yM{}Q0OoO%JAJ4>D0wIz7?DOPENReo4L9$}{M8SbZwk7qNDKs<sB*TSpELP-KV;_|^G+>ckw*Y{dMd4$*j'
    'hwh$=VTkg<)VGato#5(&Dp!o4gyy!Er{_|Y5#j`+9oM*B>H6!AEmAO?#m-Z&$McP8Se8T1O|ED9wmh7qEv@KRI{NYEAx~`uqu}!U'
    '-2V6b`b4Rxg?)1C?Jp0GCwkcjWVSt9{c)h)t_kz-&+C1>I^UNY;U81n3)qDJ1%RbY#{'
)


@lru_cache(maxsize=1)
def _templates() -> tuple[tuple[str, bytes], ...]:
    pixels = zlib.decompress(base64.b85decode(_TEMPLATE_DATA))
    if len(pixels) != len(_TEMPLATE_LABELS) * SIGNATURE_PIXELS:
        raise ValueError("Invalid bundled gene glyph templates.")
    return tuple(
        (gene, pixels[index * SIGNATURE_PIXELS:(index + 1) * SIGNATURE_PIXELS])
        for index, gene in enumerate(_TEMPLATE_LABELS)
    )


def match_gene_glyph(signature: bytes, color: int) -> str | None:
    """Return a confident glyph, or reject it for the ordinary OCR fallback.

    Every candidate must agree with the original color. Absolute distance
    rejects unrelated shapes; separation from the next different letter
    rejects ambiguous strokes. Two real W masks cover its raster variation,
    and cannot count as two competing letters when computing that margin.
    """
    if not isinstance(signature, bytes) or len(signature) != SIGNATURE_PIXELS or color not in (1, 2):
        return None
    return _match_signature(signature, color)


@lru_cache(maxsize=512)
def _match_signature(signature: bytes, color: int) -> str | None:
    candidates = "GYH" if color == 1 else "WX"
    scores: dict[str, float] = {}
    for gene, reference in _templates():
        if gene not in candidates:
            continue
        distance = sum(abs(a - b) for a, b in zip(signature, reference)) / (SIGNATURE_PIXELS * 255)
        scores[gene] = min(scores.get(gene, 1.0), distance)
    ranked = sorted(scores.items(), key=lambda item: item[1])
    gene, distance = ranked[0]
    if distance > _MAX_DISTANCE or ranked[1][1] - distance < _MIN_CLASS_MARGIN:
        return None
    return gene
