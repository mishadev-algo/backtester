// Logging release 0.4.9; base SHA256 647cccdfbdf817221573c4f8d6ed9fa83d76945f450e9401ff51301c0dab183d
//+------------------------------------------------------------------+
//|                                                   S-019_v4.mq5 |
//|      Volatility breakout from previous close (55/75% ADR3)      |
//+------------------------------------------------------------------+
#property copyright "RoboQuant AI"
#property version   "1.049"
#property strict
#property description "S-019: volatility breakout from the previous close."
#property description "Working timeframe is selectable; D1 matches I-019."
#property description "v0.4 limits the number of opened positions per server day."

#include "../Include/EaEventLog.mqh"

input group "S-019: setup";
input ENUM_TIMEFRAMES InpWorkingTimeframe = PERIOD_D1; // PERIOD_CURRENT uses chart TF
input int      InpADRPeriods               = 3;         // Completed bars in average range
input double   InpLongADRMultiplier        = 0.55;      // Long: Close[1] + multiplier * ADR
input double   InpShortADRMultiplier       = 0.75;      // Short: Close[1] - multiplier * ADR
input bool     InpAllowLong                = true;
input bool     InpAllowShort               = true;
input bool     InpAllowCounterTrend        = false;     // false = follow Slow EMA direction

input group "S-019: protective stop and limit entry";
input double   InpStopBufferPoints         = 1.0;       // Beyond ADR-window low/high
input double   InpLimitEntryFromStop       = 0.40;      // (0,1) = limit; 1 = market on new bar
input int      InpLimitLifetimeBars        = 1;         // Pending lifetime in working-TF bars
input double   InpTakeProfitRR             = 2.0;       // 0 disables TP

input group "S-019: indicators";
input bool     InpShowIndicators           = true;
input int      InpFastEMAPeriod            = 20;
input int      InpSlowEMAPeriod            = 50;
input int      InpADXPeriod                = 14;
input int      InpATRPeriod                = 14;
input bool     InpDrawLevels               = true;

input group "S-019: execution";
input double   InpRiskMoney                = 100.0;     // Risk to SL in account currency
input ulong    InpMagicNumber              = 190400;    // SSS VV AA = 019 04 00
input int      InpDeviationPoints          = 20;
input bool     InpPrintLog                 = true;     // Compatibility input; essential events always on, use EaLogDebug for details

input group "S-019: daily position limit (server day)";
input int      InpDailyPositionLimit       = 1;         // Maximum opened positions; 0 = unlimited

input group "S-019: Friday close (server time)";
input bool     InpUseFridayClose           = true;      // Close positions and pendings on Friday
input int      InpFridayCloseHour          = 21;
input int      InpFridayCloseMinute        = 0;

input group "S-019: entry delay randomizer";
input bool     InpUseEntryDelay            = true;      // Random delay between signal and order
input int      InpEntryDelayMinSeconds     = 10;
input int      InpEntryDelayMaxSeconds     = 60;
input int      InpEntryDelaySeed           = 0;         // 0 = seed from the current time

CEaLogTrade trade;

#include "../Include/NewsManager.mqh"
#define EA_FRIDAY_CUSTOM_INPUTS
#define CommonUseFridayClose InpUseFridayClose
#define CommonFridayCloseHour InpFridayCloseHour
#define CommonFridayCloseMinute InpFridayCloseMinute
#define CommonFridayDeletePendingOrders true
#define CommonFridayDeviationPoints InpDeviationPoints
#include "../Include/CommonFridayClose.mqh"

ENUM_TIMEFRAMES g_timeframe = PERIOD_D1;
datetime g_reference_bar_time = 0;
double g_adr = 0.0;
double g_reference_close = 0.0;
double g_long_trigger = 0.0;
double g_short_trigger = 0.0;
double g_long_stop_reference = 0.0;
double g_short_stop_reference = 0.0;
bool g_levels_ready = false;

int g_fast_ema_handle = INVALID_HANDLE;
int g_slow_ema_handle = INVALID_HANDLE;
int g_adx_handle = INVALID_HANDLE;
int g_atr_handle = INVALID_HANDLE;
bool g_visuals_added = false;
string g_fast_ema_chart_name = "";
string g_slow_ema_chart_name = "";
string g_adx_chart_name = "";
string g_atr_chart_name = "";
int g_adx_chart_window = -1;
int g_atr_chart_window = -1;
datetime g_last_news_management = 0;
bool g_timer_started = false;

// Signal snapshot waiting for its random entry delay.
int g_pending_entry_type = -1;              // -1 none, otherwise ENUM_ORDER_TYPE
datetime g_pending_entry_release = 0;
double g_pending_entry_signal_close = 0.0;
double g_pending_entry_raw_stop = 0.0;
bool g_friday_close_logged = false;
datetime g_daily_limit_logged_day = 0;

string g_object_prefix = "";

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
bool VisualRuntimeAvailable()
{
   return !MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_VISUAL_MODE);
}

int PriceDigits()
{
   return (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
}

double NormalizePrice(const double price)
{
   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = _Point;
   return NormalizeDouble(MathRound(price / tick_size) * tick_size,
                          PriceDigits());
}

double NormalizeVolumeDown(const double requested)
{
   double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(minimum <= 0.0 || maximum <= 0.0 || step <= 0.0)
      return 0.0;
   if(requested < minimum)
      return 0.0;

   double volume = MathMin(maximum, requested);
   volume = MathFloor((volume + 1e-12) / step) * step;
   if(volume < minimum)
      return 0.0;
   return NormalizeDouble(volume, 8);
}

double CalculateVolumeByRisk(const ENUM_ORDER_TYPE order_type,
                             const double entry_price,
                             const double stop_price)
{
   if(InpRiskMoney <= 0.0 || entry_price <= 0.0 || stop_price <= 0.0)
      return 0.0;

   if((order_type == ORDER_TYPE_BUY && stop_price >= entry_price) ||
      (order_type == ORDER_TYPE_SELL && stop_price <= entry_price))
   {
      EaLegacy("entry.blocked","SL_WRONG_SIDE",(string)("S-019: invalid entry/SL geometry for risk calculation. Entry=") + (string)(DoubleToString(entry_price, PriceDigits())) + (string)(" SL=") + (string)(DoubleToString(stop_price, PriceDigits())),"INFO",__FUNCTION__);
      return 0.0;
   }

   double loss_per_lot = 0.0;
   ResetLastError();
   if(!OrderCalcProfit(order_type, _Symbol, 1.0,
                       entry_price, stop_price, loss_per_lot))
   {
      EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("S-019: OrderCalcProfit failed. Trade skipped. Error ") + (string)(GetLastError()),"ERROR",__FUNCTION__);
      return 0.0;
   }

   loss_per_lot = MathAbs(loss_per_lot);
   if(loss_per_lot <= 0.0)
   {
      EaLegacy("entry.blocked","LOSS_PER_LOT_INVALID",(string)("S-019: loss per lot is invalid. Trade skipped."),"INFO",__FUNCTION__);
      return 0.0;
   }

   double raw_volume = InpRiskMoney / loss_per_lot;
   double volume = NormalizeVolumeDown(raw_volume);
   if(volume <= 0.0)
   {
      double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      double minimum_risk = minimum * loss_per_lot;
      EaLegacy("entry.blocked","MIN_VOLUME",(string)("S-019: calculated volume ") + (string)(DoubleToString(raw_volume, 8)) + (string)(" is below broker minimum ") + (string)(DoubleToString(minimum, 8)) + (string)(". Minimum-lot risk would be ") + (string)(DoubleToString(minimum_risk, 2)) + (string)(" account currency; target risk is ") + (string)(DoubleToString(InpRiskMoney, 2)) + (string)(". Trade skipped."),"INFO",__FUNCTION__);
      return 0.0;
   }

   if(true)
      EaLegacy("diagnostic.detail","RISK_DETAIL",(string)("S-019 risk sizing: target=") + (string)(DoubleToString(InpRiskMoney, 2)) + (string)(" lossPerLot=") + (string)(DoubleToString(loss_per_lot, 2)) + (string)(" rawVolume=") + (string)(DoubleToString(raw_volume, 8)) + (string)(" normalizedVolume=") + (string)(DoubleToString(volume, 8)) + (string)(" estimatedRisk=") + (string)(DoubleToString(volume * loss_per_lot, 2)),"INFO",__FUNCTION__);
   EaRisk(InpRiskMoney,volume*loss_per_lot,raw_volume,volume,entry_price,stop_price);
   return volume;
}

bool TradeSucceeded(const bool request_accepted)
{
   if(!request_accepted)
      return false;
   uint retcode = trade.ResultRetcode();
   return retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_DONE_PARTIAL ||
          retcode == TRADE_RETCODE_PLACED ||
          retcode == TRADE_RETCODE_NO_CHANGES;
}

void LogTradeFailure(const string action)
{
   EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("S-019: ") + (string)(action) + (string)(" failed. Retcode ") + (string)(trade.ResultRetcode()) + (string)(" ") + (string)(trade.ResultRetcodeDescription()),"ERROR",__FUNCTION__);
}

int OwnPositionType()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
         return (int)PositionGetInteger(POSITION_TYPE);
   }
   return -1;
}

ENUM_ORDER_TYPE_FILLING MarketDealFillingMode(const string symbol)
{
   long allowed_modes = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((allowed_modes & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   if((allowed_modes & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

bool ClosePositionWithReason(const ulong ticket,
                             const string close_reason,
                             MqlTradeResult &result)
{
   ZeroMemory(result);
   if(!PositionSelectByTicket(ticket))
      return true;

   string symbol = PositionGetString(POSITION_SYMBOL);
   ENUM_POSITION_TYPE position_type =
      (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return false;

   MqlTradeRequest request = {};
   request.action = TRADE_ACTION_DEAL;
   request.position = ticket;
   request.symbol = symbol;
   request.magic = InpMagicNumber;
   request.volume = PositionGetDouble(POSITION_VOLUME);
   request.deviation = InpDeviationPoints;
   request.type = position_type == POSITION_TYPE_BUY
                  ? ORDER_TYPE_SELL
                  : ORDER_TYPE_BUY;
   request.price = position_type == POSITION_TYPE_BUY ? tick.bid : tick.ask;
   request.type_filling = MarketDealFillingMode(symbol);
   request.comment = close_reason;

   if(!EaOrderSend(request, result))
      return false;
   return result.retcode == TRADE_RETCODE_DONE ||
          result.retcode == TRADE_RETCODE_DONE_PARTIAL ||
          result.retcode == TRADE_RETCODE_PLACED;
}

bool CloseOwnPositions(const string close_reason)
{
   EaActionReason(close_reason);
   bool all_closed = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      MqlTradeResult result;
      bool accepted = ClosePositionWithReason(ticket, close_reason, result);
      if(!accepted || PositionSelectByTicket(ticket))
      {
         all_closed = false;
         EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("S-019: close position #") + (string)(ticket) + (string)(" failed. Retcode ") + (string)(result.retcode) + (string)(" ") + (string)(result.comment) + (string)(". Reason: ") + (string)(close_reason),"ERROR",__FUNCTION__);
      }
   }
   return all_closed;
}

datetime ServerDayStart(const datetime now)
{
   MqlDateTime day;
   TimeToStruct(now, day);
   day.hour = 0;
   day.min = 0;
   day.sec = 0;
   return StructToTime(day);
}

// Counts unique position identifiers, so partial fills of one order do not
// consume several daily slots. The day boundary is broker server midnight.
int CountTodayOpenedPositions(const datetime now)
{
   datetime day_start = ServerDayStart(now);
   ResetLastError();
   if(day_start <= 0 || !HistorySelect(day_start, now))
   {
      EaLegacy("data.error","API_OR_BROKER_ERROR",(string)("S-019 v0.4: could not read today's deal history. Error ") + (string)(GetLastError()) + (string)(". New entries are blocked for safety."),"ERROR",__FUNCTION__);
      return -1;
   }

   ulong position_ids[];
   int position_count = 0;
   int deal_count = HistoryDealsTotal();
   for(int i = 0; i < deal_count; i++)
   {
      ulong deal_ticket = HistoryDealGetTicket(i);
      if(deal_ticket == 0 ||
         HistoryDealGetString(deal_ticket, DEAL_SYMBOL) != _Symbol ||
         (ulong)HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != InpMagicNumber)
         continue;

      ENUM_DEAL_ENTRY entry =
         (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
      ENUM_DEAL_TYPE type =
         (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
      if((entry != DEAL_ENTRY_IN && entry != DEAL_ENTRY_INOUT) ||
         (type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL))
         continue;

      ulong position_id =
         (ulong)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
      if(position_id == 0)
         position_id = deal_ticket;

      bool already_counted = false;
      for(int j = 0; j < position_count; j++)
      {
         if(position_ids[j] == position_id)
         {
            already_counted = true;
            break;
         }
      }
      if(already_counted)
         continue;

      ArrayResize(position_ids, position_count + 1);
      position_ids[position_count++] = position_id;
   }
   return position_count;
}

bool EnforceDailyPositionLimit(const datetime now)
{
   if(InpDailyPositionLimit == 0)
      return false;

   int opened_today = CountTodayOpenedPositions(now);
   if(opened_today >= 0 && opened_today < InpDailyPositionLimit)
      return false;

   CancelPendingEntry("daily position limit reached");
   DeleteOwnPendingOrders();

   datetime day_start = ServerDayStart(now);
   if(g_daily_limit_logged_day != day_start)
   {
      g_daily_limit_logged_day = day_start;
      if(opened_today < 0)
         EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("S-019 v0.4: daily position limit state is unavailable. ") + (string)("New entries are blocked."),"ERROR",__FUNCTION__);
      else if(true)
         EaLegacy("entry.blocked","DAILY_LIMIT",(string)("S-019 v0.4: daily position limit reached: ") + (string)(opened_today) + (string)("/") + (string)(InpDailyPositionLimit) + (string)(". New entries are blocked until server midnight."),"INFO",__FUNCTION__);
   }
   return true;
}

bool DeleteOwnPendingOrders()
{
   EaActionReason("NEW_SIGNAL_OR_EA_STOP");
   bool all_deleted = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 ||
         OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != InpMagicNumber)
         continue;

      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(type != ORDER_TYPE_BUY_LIMIT && type != ORDER_TYPE_SELL_LIMIT)
         continue;

      bool accepted = trade.OrderDelete(ticket);
      if(!TradeSucceeded(accepted))
      {
         all_deleted = false;
         LogTradeFailure("delete pending order #" + (string)ticket);
      }
   }
   return all_deleted;
}

bool DeleteExpiredPendingOrders()
{
   EaActionReason("LIMIT_LIFETIME");
   bool all_deleted = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 ||
         OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != InpMagicNumber)
         continue;

      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(type != ORDER_TYPE_BUY_LIMIT && type != ORDER_TYPE_SELL_LIMIT)
         continue;

      datetime setup_time = (datetime)OrderGetInteger(ORDER_TIME_SETUP);
      int age_bars = setup_time > 0
                     ? iBarShift(_Symbol, g_timeframe, setup_time, false)
                     : -1;
      if(age_bars >= 0 && age_bars < InpLimitLifetimeBars)
         continue;

      bool accepted = trade.OrderDelete(ticket);
      if(!TradeSucceeded(accepted))
      {
         all_deleted = false;
         LogTradeFailure("delete expired pending order #" + (string)ticket);
      }
      else if(true)
         EaLegacy("diagnostic.detail","DETAIL",(string)("S-019 v0.4: pending order #") + (string)(ticket) + (string)(" expired after ") + (string)(InpLimitLifetimeBars) + (string)(" working-TF bar(s)."),"INFO",__FUNCTION__);
   }
   return all_deleted;
}

double BrokerValidStop(const ENUM_ORDER_TYPE order_type,
                       const double raw_stop)
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = _Point;
   double minimum_distance = MathMax(
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point,
      tick_size);

   double stop = raw_stop;
   if(order_type == ORDER_TYPE_BUY)
   {
      stop = MathMin(stop, bid - minimum_distance);
      stop = MathFloor(stop / tick_size) * tick_size;
   }
   else
   {
      stop = MathMax(stop, ask + minimum_distance);
      stop = MathCeil(stop / tick_size) * tick_size;
   }

   return NormalizeDouble(stop, PriceDigits());
}

double BrokerValidTakeProfit(const ENUM_ORDER_TYPE order_type,
                             const double raw_take_profit)
{
   if(raw_take_profit <= 0.0)
      return 0.0;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = _Point;
   double minimum_distance = MathMax(
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point,
      tick_size);

   double take_profit = raw_take_profit;
   if(order_type == ORDER_TYPE_BUY)
   {
      take_profit = MathMax(take_profit, bid + minimum_distance);
      take_profit = MathCeil(take_profit / tick_size) * tick_size;
   }
   else
   {
      take_profit = MathMin(take_profit, ask - minimum_distance);
      take_profit = MathFloor(take_profit / tick_size) * tick_size;
   }
   return NormalizeDouble(take_profit, PriceDigits());
}

// Same TP protection as NewsManager, but the exceptional market close gets
// an S-019-specific deal comment. Keep the public NewsManager API unchanged.
void ManageNewsTakeProfitsWithReason(const datetime now,
                                     const ulong magic_number)
{
   CleanupStoredNewsTps(now, magic_number);
   bool remove_tp = UseNewsFilter && IsNewsTpRemovalWindow(now);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != magic_number)
         continue;

      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      if(remove_tp)
      {
         if(tp > 0.0)
         {
            if(!StoreNewsTp(ticket, tp, magic_number))
               continue;

            bool accepted = trade.PositionModify(ticket, sl, 0.0);
            if(!IsNewsTradeRequestAccepted(accepted) ||
               !PositionHasNewsTp(ticket, 0.0))
               EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("Could not remove TP before news. Ticket ") + (string)(ticket) + (string)(". Retcode: ") + (string)(trade.ResultRetcode()) + (string)(" ") + (string)(trade.ResultRetcodeDescription()),"ERROR",__FUNCTION__);
         }
         continue;
      }

      double saved_tp = 0.0;
      if(!GetStoredNewsTp(ticket, magic_number, saved_tp))
         continue;

      long position_type = PositionGetInteger(POSITION_TYPE);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      bool price_passed_tp =
         (position_type == POSITION_TYPE_BUY && bid >= saved_tp) ||
         (position_type == POSITION_TYPE_SELL && ask <= saved_tp);
      if(price_passed_tp)
      {
         MqlTradeResult close_result;
         bool accepted = ClosePositionWithReason(
            ticket, "S-019: news-protected TP", close_result);
         bool position_closed = !PositionSelectByTicket(ticket);
         if(accepted && position_closed)
         {
            EaLegacy("management.detail","MANAGEMENT_RESULT_DETAIL",(string)("Position ") + (string)(ticket) + (string)(" closed at market: price passed TP while it was removed for news."),"INFO",__FUNCTION__);
            RemoveStoredNewsTp(ticket, magic_number);
         }
         else
            EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("Could not close position after news. Ticket ") + (string)(ticket) + (string)(". Retcode: ") + (string)(close_result.retcode) + (string)(" ") + (string)(close_result.comment),"ERROR",__FUNCTION__);
         continue;
      }

      bool accepted = trade.PositionModify(ticket, sl, saved_tp);
      if(IsNewsTradeRequestAccepted(accepted) &&
         PositionHasNewsTp(ticket, saved_tp))
      {
         EaLegacy("management.detail","POSITION_MANAGEMENT",(string)("TP restored after news. Ticket ") + (string)(ticket) + (string)(" TP: ") + (string)(saved_tp),"INFO",__FUNCTION__);
         RemoveStoredNewsTp(ticket, magic_number);
      }
      else
         EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("Could not restore TP after news. Ticket ") + (string)(ticket) + (string)(". Retcode: ") + (string)(trade.ResultRetcode()) + (string)(" ") + (string)(trade.ResultRetcodeDescription()),"ERROR",__FUNCTION__);
   }
}

void ManageNewsProtectionThrottled(const datetime now)
{
   if(InpTakeProfitRR <= 0.0 || !UseNewsFilter ||
      now <= 0 || now == g_last_news_management || OwnPositionType() == -1)
      return;
   g_last_news_management = now;
   ManageNewsTakeProfitsWithReason(now, InpMagicNumber);
}

//+------------------------------------------------------------------+
//| Reference levels                                                 |
//+------------------------------------------------------------------+
bool CalculateLevels()
{
   if(Bars(_Symbol, g_timeframe) < InpADRPeriods + 2)
      { EaWait("ADR_HISTORY_TOO_SHORT","","{}","data",""); return false; }

   double range_sum = 0.0;
   double window_low = DBL_MAX;
   double window_high = -DBL_MAX;

   for(int shift = 1; shift <= InpADRPeriods; shift++)
   {
      datetime bar_time = iTime(_Symbol, g_timeframe, shift);
      double high = iHigh(_Symbol, g_timeframe, shift);
      double low = iLow(_Symbol, g_timeframe, shift);
      if(bar_time == 0 || high <= low || low <= 0.0)
         { EaWait("ADR_BAR_INVALID","","{}","data",""); return false; }

      range_sum += high - low;
      window_low = MathMin(window_low, low);
      window_high = MathMax(window_high, high);
   }

   double previous_close = iClose(_Symbol, g_timeframe, 1);
   if(previous_close <= 0.0)
      { EaWait("REFERENCE_CLOSE_UNAVAILABLE","","{}","data",""); return false; }

   g_adr = range_sum / InpADRPeriods;
   g_reference_close = previous_close;
   g_long_trigger = NormalizePrice(previous_close +
                                   InpLongADRMultiplier * g_adr);
   g_short_trigger = NormalizePrice(previous_close -
                                    InpShortADRMultiplier * g_adr);
   g_long_stop_reference = NormalizePrice(window_low -
                                          InpStopBufferPoints * _Point);
   g_short_stop_reference = NormalizePrice(window_high +
                                           InpStopBufferPoints * _Point);
   g_reference_bar_time = iTime(_Symbol, g_timeframe, 0);
   g_levels_ready = g_long_trigger > g_short_trigger && g_adr > 0.0;
   if(g_levels_ready) EaSetup(g_reference_bar_time,"","ADR_LEVELS",
      "{\"reference_close\":"+ELN(g_reference_close)+",\"adr\":"+ELN(g_adr)+",\"adr_bars\":"+(string)InpADRPeriods+",\"long_trigger\":"+ELN(g_long_trigger)+",\"short_trigger\":"+ELN(g_short_trigger)+",\"long_sl_reference\":"+ELN(g_long_stop_reference)+",\"short_sl_reference\":"+ELN(g_short_stop_reference)+"}");

   if(g_levels_ready && true)
      EaLegacy("diagnostic.detail","ADR_LEVELS_DETAIL",(string)("S-019 levels: TF=") + (string)(EnumToString(g_timeframe)) + (string)(" Close[1]=") + (string)(DoubleToString(g_reference_close, PriceDigits())) + (string)(" ADR") + (string)(InpADRPeriods) + (string)("=") + (string)(DoubleToString(g_adr, PriceDigits())) + (string)(" long=") + (string)(DoubleToString(g_long_trigger, PriceDigits())) + (string)(" short=") + (string)(DoubleToString(g_short_trigger, PriceDigits())) + (string)(" longSL=") + (string)(DoubleToString(g_long_stop_reference, PriceDigits())) + (string)(" shortSL=") + (string)(DoubleToString(g_short_stop_reference, PriceDigits())),"INFO",__FUNCTION__);

   return g_levels_ready;
}

//+------------------------------------------------------------------+
//| Chart visuals                                                    |
//+------------------------------------------------------------------+
void SetHorizontalLine(const string suffix,
                       const double price,
                       const color line_color,
                       const ENUM_LINE_STYLE style)
{
   string name = g_object_prefix + suffix;
   if(ObjectFind(0, name) < 0)
   {
      if(!ObjectCreate(0, name, OBJ_HLINE, 0, 0, price))
         return;
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   ObjectSetDouble(0, name, OBJPROP_PRICE, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, line_color);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
}

void DrawLevels()
{
   if(!InpDrawLevels || !g_levels_ready || !VisualRuntimeAvailable())
      return;
   SetHorizontalLine("LONG_TRIGGER", g_long_trigger, clrLimeGreen, STYLE_SOLID);
   SetHorizontalLine("SHORT_TRIGGER", g_short_trigger, clrTomato, STYLE_SOLID);
   SetHorizontalLine("LONG_STOP", g_long_stop_reference, clrDeepSkyBlue, STYLE_DOT);
   SetHorizontalLine("SHORT_STOP", g_short_stop_reference, clrOrange, STYLE_DOT);
   ChartRedraw(0);
}

void DeleteLevelObjects()
{
   ObjectDelete(0, g_object_prefix + "LONG_TRIGGER");
   ObjectDelete(0, g_object_prefix + "SHORT_TRIGGER");
   ObjectDelete(0, g_object_prefix + "LONG_STOP");
   ObjectDelete(0, g_object_prefix + "SHORT_STOP");
}

bool CreateVisualIndicators()
{
   // Fast EMA, ADX, ATR and chart attachment are visual only. The Slow EMA
   // trading handle is created separately in OnInit().
   if(!InpShowIndicators || !VisualRuntimeAvailable())
      return true;

   g_fast_ema_handle = iMA(_Symbol, g_timeframe, InpFastEMAPeriod, 0,
                           MODE_EMA, PRICE_CLOSE);
   g_adx_handle = iADX(_Symbol, g_timeframe, InpADXPeriod);
   g_atr_handle = iATR(_Symbol, g_timeframe, InpATRPeriod);
   if(g_fast_ema_handle == INVALID_HANDLE ||
      g_slow_ema_handle == INVALID_HANDLE ||
      g_adx_handle == INVALID_HANDLE ||
      g_atr_handle == INVALID_HANDLE)
   {
      EaLegacy("data.error","API_OR_BROKER_ERROR",(string)("S-019: failed to create EMA/ADX/ATR indicator handles. Error ") + (string)(GetLastError()),"ERROR",__FUNCTION__);
      return false;
   }

   // The visual Strategy Tester automatically displays indicator handles
   // created by an EA. Calling ChartIndicatorAdd() there would display ADX,
   // ATR and the EMAs a second time. Manual attachment is only for live/demo
   // charts; the tester owns and removes its automatically shown instances.
   if(MQLInfoInteger(MQL_TESTER) && MQLInfoInteger(MQL_VISUAL_MODE))
   {
      g_visuals_added = true;
      return true;
   }

   int fast_index = ChartIndicatorsTotal(0, 0);
   bool fast_added = ChartIndicatorAdd(0, 0, g_fast_ema_handle);
   if(fast_added)
      g_fast_ema_chart_name = ChartIndicatorName(0, 0, fast_index);

   int slow_index = ChartIndicatorsTotal(0, 0);
   bool slow_added = ChartIndicatorAdd(0, 0, g_slow_ema_handle);
   if(slow_added)
      g_slow_ema_chart_name = ChartIndicatorName(0, 0, slow_index);

   int adx_window = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);
   bool adx_added = ChartIndicatorAdd(0, adx_window, g_adx_handle);
   if(adx_added)
   {
      g_adx_chart_window = adx_window;
      g_adx_chart_name = ChartIndicatorName(0, adx_window, 0);
   }

   int atr_window = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);
   bool atr_added = ChartIndicatorAdd(0, atr_window, g_atr_handle);
   if(atr_added)
   {
      g_atr_chart_window = atr_window;
      g_atr_chart_name = ChartIndicatorName(0, atr_window, 0);
   }
   g_visuals_added = fast_added && slow_added && adx_added && atr_added;

   if(!g_visuals_added)
      EaLegacy("data.error","API_OR_BROKER_ERROR",(string)("S-019: EMA/ADX/ATR could not be attached to this chart. The chart TF ") + (string)("must match InpWorkingTimeframe (") + (string)(EnumToString(g_timeframe)) + (string)("). Trading logic remains active. Error ") + (string)(GetLastError()),"ERROR",__FUNCTION__);
   return true;
}

void ReleaseVisualIndicators()
{
   // ChartIndicatorAdd attaches a separate chart instance. Remove that
   // instance before releasing the handle, otherwise EA reinitialization can
   // leave old EMA/ADX/ATR instances and add another set on the next OnInit().
   if(g_atr_chart_window >= 0 && g_atr_chart_name != "")
      ChartIndicatorDelete(0, g_atr_chart_window, g_atr_chart_name);
   if(g_adx_chart_window >= 0 && g_adx_chart_name != "")
      ChartIndicatorDelete(0, g_adx_chart_window, g_adx_chart_name);
   if(g_slow_ema_chart_name != "")
      ChartIndicatorDelete(0, 0, g_slow_ema_chart_name);
   if(g_fast_ema_chart_name != "")
      ChartIndicatorDelete(0, 0, g_fast_ema_chart_name);

   if(g_fast_ema_handle != INVALID_HANDLE)
      IndicatorRelease(g_fast_ema_handle);
   if(g_slow_ema_handle != INVALID_HANDLE)
      IndicatorRelease(g_slow_ema_handle);
   if(g_adx_handle != INVALID_HANDLE)
      IndicatorRelease(g_adx_handle);
   if(g_atr_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_handle);
   g_fast_ema_handle = INVALID_HANDLE;
   g_slow_ema_handle = INVALID_HANDLE;
   g_adx_handle = INVALID_HANDLE;
   g_atr_handle = INVALID_HANDLE;
   g_fast_ema_chart_name = "";
   g_slow_ema_chart_name = "";
   g_adx_chart_name = "";
   g_atr_chart_name = "";
   g_adx_chart_window = -1;
   g_atr_chart_window = -1;
}

//+------------------------------------------------------------------+
//| Limit entry and reversal after a confirmed bar close            |
//+------------------------------------------------------------------+
double StopForPendingEntry(const ENUM_ORDER_TYPE order_type,
                           const double raw_stop,
                           const double entry_price)
{
   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = _Point;
   double minimum_distance = MathMax(
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point,
      tick_size);

   double stop = raw_stop;
   if(order_type == ORDER_TYPE_BUY)
   {
      stop = MathMin(stop, entry_price - minimum_distance);
      stop = MathFloor(stop / tick_size) * tick_size;
   }
   else
   {
      stop = MathMax(stop, entry_price + minimum_distance);
      stop = MathCeil(stop / tick_size) * tick_size;
   }
   return NormalizeDouble(stop, PriceDigits());
}

double TakeProfitForPendingEntry(const ENUM_ORDER_TYPE order_type,
                                 const double raw_take_profit,
                                 const double entry_price)
{
   if(raw_take_profit <= 0.0)
      return 0.0;

   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = _Point;
   double minimum_distance = MathMax(
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point,
      tick_size);

   double take_profit = raw_take_profit;
   if(order_type == ORDER_TYPE_BUY)
   {
      take_profit = MathMax(take_profit, entry_price + minimum_distance);
      take_profit = MathCeil(take_profit / tick_size) * tick_size;
   }
   else
   {
      take_profit = MathMin(take_profit, entry_price - minimum_distance);
      take_profit = MathFloor(take_profit / tick_size) * tick_size;
   }
   return NormalizeDouble(take_profit, PriceDigits());
}

bool LimitPriceIsPlaceable(const ENUM_ORDER_TYPE order_type,
                           const double entry_price)
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return false;

   double minimum_distance = MathMax(
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL)) * _Point;

   if(order_type == ORDER_TYPE_BUY)
      return entry_price < tick.ask && tick.ask - entry_price >= minimum_distance;
   return entry_price > tick.bid && entry_price - tick.bid >= minimum_distance;
}

bool PlaceLimitOrder(const ENUM_ORDER_TYPE order_type,
                     const double signal_close,
                     const double raw_stop)
{
   if(OwnPositionType()!=-1) { EaBlock("POSITION_EXISTS"); return false; }
   if(CommonFridayCloseWindow(TimeCurrent())) { EaBlock("FRIDAY_CUTOFF"); return false; }
   if(IsNewsEntryBlocked(TimeCurrent())) { EaBlock("NEWS_BLOCK"); return false; }
   double signal_to_stop = order_type == ORDER_TYPE_BUY
                           ? signal_close - raw_stop
                           : raw_stop - signal_close;
   if(signal_to_stop <= 0.0)
   {
      EaLegacy("entry.blocked","SL_WRONG_SIDE",(string)("S-019 v0.4: invalid signal-to-stop distance. Limit skipped."),"INFO",__FUNCTION__);
      return false;
   }

   // N is measured from the stop toward the signal close. Example: a
   // 100-point range and N=0.4 place the limit 40 points from the stop.
   double raw_entry = order_type == ORDER_TYPE_BUY
                      ? raw_stop + InpLimitEntryFromStop * signal_to_stop
                      : raw_stop - InpLimitEntryFromStop * signal_to_stop;
   double entry_price = NormalizePrice(raw_entry);
   double stop = StopForPendingEntry(order_type, raw_stop, entry_price);
   double risk_distance = order_type == ORDER_TYPE_BUY
                           ? entry_price - stop
                           : stop - entry_price;
   if(risk_distance <= 0.0)
   {
      EaLegacy("entry.blocked","SL_WRONG_SIDE",(string)("S-019 v0.4: invalid limit-entry risk distance."),"INFO",__FUNCTION__);
      return false;
   }

   if(!LimitPriceIsPlaceable(order_type, entry_price))
   {
      EaLegacy("entry.blocked","ENTRY_FILTER",(string)("S-019 v0.4: calculated ") + (string)(order_type == ORDER_TYPE_BUY ? "Buy Limit" : "Sell Limit") + (string)(" is already reached or violates broker distance. Entry=") + (string)(DoubleToString(entry_price, PriceDigits())) + (string)(". Order skipped."),"INFO",__FUNCTION__);
      return false;
   }

   double raw_take_profit = 0.0;
   if(InpTakeProfitRR > 0.0)
      raw_take_profit = order_type == ORDER_TYPE_BUY
                        ? entry_price + InpTakeProfitRR * risk_distance
                        : entry_price - InpTakeProfitRR * risk_distance;
   double take_profit = TakeProfitForPendingEntry(order_type,
                                                  raw_take_profit,
                                                  entry_price);

   double volume = CalculateVolumeByRisk(order_type, entry_price, stop);
   if(volume <= 0.0)
      { EaBlock("RISK_VOLUME_INVALID","","{}","entry",""); return false; }

   bool accepted = false;
   if(order_type == ORDER_TYPE_BUY)
      accepted = trade.BuyLimit(volume, entry_price, _Symbol, stop, take_profit,
                                ORDER_TIME_GTC, 0, "S-019 v0.4 long limit");
   else
      accepted = trade.SellLimit(volume, entry_price, _Symbol, stop, take_profit,
                                 ORDER_TIME_GTC, 0, "S-019 v0.4 short limit");

   if(!TradeSucceeded(accepted))
   {
      LogTradeFailure(order_type == ORDER_TYPE_BUY
                      ? "place buy limit"
                      : "place sell limit");
      return false;
   }

   if(true)
      EaLegacy("entry.detail","REQUEST_ACCEPTED_DETAIL",(string)("S-019 v0.4: placed ") + (string)(order_type == ORDER_TYPE_BUY ? "BUY LIMIT" : "SELL LIMIT") + (string)(" volume=") + (string)(DoubleToString(volume, 8)) + (string)(" Entry=") + (string)(DoubleToString(entry_price, PriceDigits())) + (string)(" SL=") + (string)(DoubleToString(stop, PriceDigits())) + (string)(" TP=") + (string)(DoubleToString(take_profit, PriceDigits())) + (string)(" RR=") + (string)(DoubleToString(InpTakeProfitRR, 2)),"INFO",__FUNCTION__);
   return true;
}

bool OpenMarketPosition(const ENUM_ORDER_TYPE order_type,
                        const double raw_stop)
{
   if(OwnPositionType()!=-1) { EaBlock("POSITION_EXISTS"); return false; }
   if(CommonFridayCloseWindow(TimeCurrent())) { EaBlock("FRIDAY_CUTOFF"); return false; }
   if(IsNewsEntryBlocked(TimeCurrent())) { EaBlock("NEWS_BLOCK"); return false; }
   double stop = BrokerValidStop(order_type, raw_stop);
   double entry_price = order_type == ORDER_TYPE_BUY
                        ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                        : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double risk_distance = order_type == ORDER_TYPE_BUY
                          ? entry_price - stop
                          : stop - entry_price;
   if(risk_distance <= 0.0)
   {
      EaLegacy("operation.error","INVALID_SESSION_TIME",(string)("S-019 v0.4: invalid market-entry risk distance."),"ERROR",__FUNCTION__);
      return false;
   }

   double raw_take_profit = 0.0;
   if(InpTakeProfitRR > 0.0)
      raw_take_profit = order_type == ORDER_TYPE_BUY
                        ? entry_price + InpTakeProfitRR * risk_distance
                        : entry_price - InpTakeProfitRR * risk_distance;
   double take_profit = BrokerValidTakeProfit(order_type, raw_take_profit);

   double volume = CalculateVolumeByRisk(order_type, entry_price, stop);
   if(volume <= 0.0)
      { EaBlock("RISK_VOLUME_INVALID","","{}","entry",""); return false; }

   bool accepted = order_type == ORDER_TYPE_BUY
                   ? trade.Buy(volume, _Symbol, 0.0, stop, take_profit,
                               "S-019 v0.4 long market")
                   : trade.Sell(volume, _Symbol, 0.0, stop, take_profit,
                                "S-019 v0.4 short market");
   if(!TradeSucceeded(accepted))
   {
      LogTradeFailure(order_type == ORDER_TYPE_BUY
                      ? "open market buy"
                      : "open market sell");
      return false;
   }

   if(true)
      EaLegacy("entry.detail","REQUEST_ACCEPTED_DETAIL",(string)("S-019 v0.4: opened ") + (string)(order_type == ORDER_TYPE_BUY ? "BUY MARKET" : "SELL MARKET") + (string)(" volume=") + (string)(DoubleToString(volume, 8)) + (string)(" Entry=") + (string)(DoubleToString(entry_price, PriceDigits())) + (string)(" SL=") + (string)(DoubleToString(stop, PriceDigits())) + (string)(" TP=") + (string)(DoubleToString(take_profit, PriceDigits())) + (string)(" RR=") + (string)(DoubleToString(InpTakeProfitRR, 2)),"INFO",__FUNCTION__);
   return true;
}

bool SubmitEntry(const ENUM_ORDER_TYPE order_type,
                 const double signal_close,
                 const double raw_stop)
{
   if(OwnPositionType()!=-1) { EaBlock("POSITION_EXISTS"); return false; }
   if(CommonFridayCloseWindow(TimeCurrent())) { EaBlock("FRIDAY_CUTOFF"); return false; }
   if(IsNewsEntryBlocked(TimeCurrent())) { EaBlock("NEWS_BLOCK"); return false; }
   if(EnforceDailyPositionLimit(TimeCurrent()))
      return false;
   if(InpLimitEntryFromStop >= 1.0)
      return OpenMarketPosition(order_type, raw_stop);
   return PlaceLimitOrder(order_type, signal_close, raw_stop);
}

//+------------------------------------------------------------------+
//| Friday close and random entry delay                              |
//+------------------------------------------------------------------+
void InitEntryDelayRandomizer()
{
   if(InpEntryDelaySeed > 0)
      MathSrand(InpEntryDelaySeed);
   else
      MathSrand((int)(GetTickCount() ^ (uint)TimeLocal()));
}

int RandomIntInRange(const int minimum, const int maximum)
{
   int low = MathMin(minimum, maximum);
   int high = MathMax(minimum, maximum);
   if(low >= high)
      return low;
   return low + (int)(MathRand() % (high - low + 1));
}

void ResetPendingEntry()
{
   EaClearPending();
   g_pending_entry_type = -1;
   g_pending_entry_release = 0;
   g_pending_entry_signal_close = 0.0;
   g_pending_entry_raw_stop = 0.0;
}

void CancelPendingEntry(const string reason)
{
   if(g_pending_entry_type>=0) EaUsePending();
   if(g_pending_entry_type < 0)
      return;
   EaLegacy("entry.cancelled","DELAY_CANCELLED",(string)("S-019 v0.4: delayed ") + (string)(g_pending_entry_type == ORDER_TYPE_BUY ? "long" : "short") + (string)(" entry cancelled: ") + (string)(reason),"INFO",__FUNCTION__);
   ResetPendingEntry();
}

// Accepts a confirmed signal and stores it until its random delay elapses.
// The stop reference is captured here so a later level recalculation cannot
// move the protective stop of an already accepted signal.
void QueueEntry(const ENUM_ORDER_TYPE order_type,
                const double signal_close,
                const double raw_stop)
{
   if(OwnPositionType()!=-1) { EaBlock("POSITION_EXISTS"); return ; }
   if(CommonFridayCloseWindow(TimeCurrent())) { EaBlock("FRIDAY_CUTOFF"); return ; }
   if(IsNewsEntryBlocked(TimeCurrent())) { EaBlock("NEWS_BLOCK"); return ; }
   if(EnforceDailyPositionLimit(TimeCurrent()))
      return;

   if(!InpUseEntryDelay)
   {
      SubmitEntry(order_type, signal_close, raw_stop);
      return;
   }

   // One signal at a time.
   if(g_pending_entry_type >= 0)
      { EaBlock("DELAY_ALREADY_PENDING","","{}","entry",""); return; }

   int delay_seconds = RandomIntInRange(InpEntryDelayMinSeconds,
                                        InpEntryDelayMaxSeconds);
   EaSavePending();
   g_pending_entry_type = (int)order_type;
   g_pending_entry_release = TimeCurrent() + delay_seconds;
   g_pending_entry_signal_close = signal_close;
   g_pending_entry_raw_stop = raw_stop;

   if(true)
      EaLegacy("entry.queued","RANDOM_DELAY",(string)("S-019 v0.4: ") + (string)(order_type == ORDER_TYPE_BUY ? "long" : "short") + (string)(" signal accepted. Entry delay ") + (string)(delay_seconds) + (string)(" s, order is sent at ") + (string)(TimeToString(g_pending_entry_release, TIME_DATE | TIME_SECONDS)),"INFO",__FUNCTION__);
}

// Sends the stored order once the delay elapsed. The signal is not
// revalidated: if there was a signal, the order goes out regardless of where
// price travelled during the delay.
void ProcessPendingEntry()
{
   if(g_pending_entry_type < 0 || TimeCurrent() < g_pending_entry_release)
      return;
   EaUsePending();

   if(OwnPositionType()!=-1) { EaBlock("POSITION_EXISTS","Delay cancelled"); CancelPendingEntry("position appeared during delay"); return; }
   if(CommonFridayCloseWindow(TimeCurrent())) { EaBlock("FRIDAY_CUTOFF","Delay cancelled"); CancelPendingEntry("Friday cutoff during delay"); return; }
   if(IsNewsEntryBlocked(TimeCurrent())) { EaBlock("NEWS_BLOCK","Delay cancelled"); CancelPendingEntry("news block during delay"); return; }
   ENUM_ORDER_TYPE order_type = (ENUM_ORDER_TYPE)g_pending_entry_type;
   double signal_close = g_pending_entry_signal_close;
   double raw_stop = g_pending_entry_raw_stop;
   ResetPendingEntry();

   if(true)
      EaLegacy("entry.released","DELAY_ELAPSED",(string)("S-019 v0.4: sending delayed ") + (string)(order_type == ORDER_TYPE_BUY ? "long" : "short") + (string)(" entry."),"INFO",__FUNCTION__);
   SubmitEntry(order_type, signal_close, raw_stop);
}

// From the Friday cutoff onwards the EA closes its positions, deletes its
// pending orders and stops trading until the next week.
bool ManageFridayClose(const datetime now)
{
   if(!CommonManageFridayClose(now,InpMagicNumber)) return false;
   EaBlock("FRIDAY_CUTOFF","","{}","session");
   CancelPendingEntry("Friday cutoff"); ResetNewsDeferredEntry(); return true;
}

bool SlowEMAAllowsEntry(const bool long_signal,
                        const bool short_signal,
                        const double signal_close,
                        double &slow_ema)
{
   slow_ema = 0.0;
   if(InpAllowCounterTrend)
      return true;
   if(g_slow_ema_handle == INVALID_HANDLE)
   {
      EaLegacy("data.error","API_OR_BROKER_ERROR",(string)("S-019 v0.4: Slow EMA handle is unavailable. Entry skipped."),"ERROR",__FUNCTION__);
      return false;
   }

   double ema_buffer[1];
   ResetLastError();
   if(CopyBuffer(g_slow_ema_handle, 0, 1, 1, ema_buffer) != 1 ||
      ema_buffer[0] <= 0.0)
   {
      EaLegacy("operation.error","API_OR_BROKER_ERROR",(string)("S-019 v0.4: could not read closed-bar Slow EMA. Entry skipped. Error ") + (string)(GetLastError()),"ERROR",__FUNCTION__);
      return false;
   }

   slow_ema = ema_buffer[0];
   if(long_signal)
      return signal_close > slow_ema;
   if(short_signal)
      return signal_close < slow_ema;
   return false;
}

void ProcessClosedBarSignal(const bool long_signal,
                            const bool short_signal,
                            const double signal_close,
                            const bool trend_entry_allowed,
                            const double signal_slow_ema)
{
   if(!long_signal && !short_signal) { EaWait("CLOSE_INSIDE_ADR_LEVELS","","{\"close\":"+ELN(signal_close)+",\"long_level\":"+ELN(log_signal_long_level)+",\"short_level\":"+ELN(log_signal_short_level)+"}"); return; }
   EaSignal(iTime(_Symbol,g_timeframe,1),long_signal ? "BUY" : "SELL","ADR_CLOSE_BREAKOUT","{\"close\":"+ELN(signal_close)+",\"slow_ema\":"+ELN(signal_slow_ema)+",\"long_level_used\":"+ELN(log_signal_long_level)+",\"short_level_used\":"+ELN(log_signal_short_level)+"}");

   int position_type = OwnPositionType();
   datetime now = TimeCurrent();

   if(true)
      EaLegacy("management.detail","MANAGEMENT_RESULT_DETAIL",(string)("S-019 confirmed ") + (string)(long_signal ? "LONG" : "SHORT") + (string)(" signal: bar closed at ") + (string)(DoubleToString(signal_close, PriceDigits())) + (string)(". Entry is attempted at the new bar open."),"INFO",__FUNCTION__);

   // An opposite confirmed close always exits. News only blocks the new leg.
   if(long_signal && position_type == POSITION_TYPE_SELL)
   {
      if(!CloseOwnPositions("S-019: opposite signal"))
         return;
      position_type = -1;
   }
   else if(short_signal && position_type == POSITION_TYPE_BUY)
   {
      if(!CloseOwnPositions("S-019: opposite signal"))
         return;
      position_type = -1;
   }

   // Same-direction signals do not pyramid.
   if(position_type != -1)
      { EaBlock("POSITION_EXISTS","","{}","entry",""); return; }

   // The trend filter only blocks the new leg. An opposite position was
   // already closed above.
   if(!trend_entry_allowed)
   {
      EaLegacy("entry.blocked","TREND_FILTER",(string)("S-019 v0.4: counter-trend entry skipped. Signal close=") + (string)(DoubleToString(signal_close, PriceDigits())) + (string)(" SlowEMA=") + (string)(DoubleToString(signal_slow_ema, PriceDigits())) + (string)(" AllowCounterTrend=false"),"INFO",__FUNCTION__);
      return;
   }

   // A news-blocked open is skipped and is not deferred because entry is
   // valid only at this bar open.
   if(IsNewsEntryBlocked(now))
      { EaBlock("NEWS_BLOCK","","{}","entry",""); return; }

   if(long_signal && InpAllowLong)
      QueueEntry(ORDER_TYPE_BUY, signal_close, g_long_stop_reference);
   else if(short_signal && InpAllowShort)
      QueueEntry(ORDER_TYPE_SELL, signal_close, g_short_stop_reference);
   else EaBlock("DIRECTION_DISABLED");
}

//+------------------------------------------------------------------+
//| Expert lifecycle                                                 |
//+------------------------------------------------------------------+
int OnInit()
{
   string log_inputs="";
   log_inputs+="AutoDetectNewsCurrencies="+(string)AutoDetectNewsCurrencies+";";
   log_inputs+="CommonFridayCloseHour="+(string)CommonFridayCloseHour+";";
   log_inputs+="CommonFridayCloseMinute="+(string)CommonFridayCloseMinute+";";
   log_inputs+="CommonFridayDeletePendingOrders="+(string)CommonFridayDeletePendingOrders+";";
   log_inputs+="CommonFridayDeviationPoints="+(string)CommonFridayDeviationPoints+";";
   log_inputs+="CommonUseFridayClose="+(string)CommonUseFridayClose+";";
   log_inputs+="EaLogDebug="+(string)EaLogDebug+";";
   log_inputs+="EaLogHeartbeatMinutes="+(string)EaLogHeartbeatMinutes+";";
   log_inputs+="InpADRPeriods="+(string)InpADRPeriods+";";
   log_inputs+="InpADXPeriod="+(string)InpADXPeriod+";";
   log_inputs+="InpATRPeriod="+(string)InpATRPeriod+";";
   log_inputs+="InpAllowCounterTrend="+(string)InpAllowCounterTrend+";";
   log_inputs+="InpAllowLong="+(string)InpAllowLong+";";
   log_inputs+="InpAllowShort="+(string)InpAllowShort+";";
   log_inputs+="InpDailyPositionLimit="+(string)InpDailyPositionLimit+";";
   log_inputs+="InpDeviationPoints="+(string)InpDeviationPoints+";";
   log_inputs+="InpDrawLevels="+(string)InpDrawLevels+";";
   log_inputs+="InpEntryDelayMaxSeconds="+(string)InpEntryDelayMaxSeconds+";";
   log_inputs+="InpEntryDelayMinSeconds="+(string)InpEntryDelayMinSeconds+";";
   log_inputs+="InpEntryDelaySeed="+(string)InpEntryDelaySeed+";";
   log_inputs+="InpFastEMAPeriod="+(string)InpFastEMAPeriod+";";
   log_inputs+="InpFridayCloseHour="+(string)InpFridayCloseHour+";";
   log_inputs+="InpFridayCloseMinute="+(string)InpFridayCloseMinute+";";
   log_inputs+="InpLimitEntryFromStop="+(string)InpLimitEntryFromStop+";";
   log_inputs+="InpLimitLifetimeBars="+(string)InpLimitLifetimeBars+";";
   log_inputs+="InpLongADRMultiplier="+(string)InpLongADRMultiplier+";";
   log_inputs+="InpMagicNumber="+(string)InpMagicNumber+";";
   log_inputs+="InpPrintLog="+(string)InpPrintLog+";";
   log_inputs+="InpRiskMoney="+(string)InpRiskMoney+";";
   log_inputs+="InpShortADRMultiplier="+(string)InpShortADRMultiplier+";";
   log_inputs+="InpShowIndicators="+(string)InpShowIndicators+";";
   log_inputs+="InpSlowEMAPeriod="+(string)InpSlowEMAPeriod+";";
   log_inputs+="InpStopBufferPoints="+(string)InpStopBufferPoints+";";
   log_inputs+="InpTakeProfitRR="+(string)InpTakeProfitRR+";";
   log_inputs+="InpUseEntryDelay="+(string)InpUseEntryDelay+";";
   log_inputs+="InpUseFridayClose="+(string)InpUseFridayClose+";";
   log_inputs+="InpWorkingTimeframe="+(string)InpWorkingTimeframe+";";
   log_inputs+="NewsCurrency="+(string)NewsCurrency+";";
   log_inputs+="NewsEntryBlockMinutesAfter="+(string)NewsEntryBlockMinutesAfter+";";
   log_inputs+="NewsEntryBlockMinutesBefore="+(string)NewsEntryBlockMinutesBefore+";";
   log_inputs+="NewsExtraCurrencies="+(string)NewsExtraCurrencies+";";
   log_inputs+="NewsMinimumImportance="+(string)NewsMinimumImportance+";";
   log_inputs+="NewsTpRemoveMinutesBefore="+(string)NewsTpRemoveMinutesBefore+";";
   log_inputs+="NewsTpRestoreMinutesAfter="+(string)NewsTpRestoreMinutesAfter+";";
   log_inputs+="UseNewsFilter="+(string)UseNewsFilter+";";
   EaLogInit("S-019","0.4.9",(ulong)InpMagicNumber,InpWorkingTimeframe,log_inputs);

   g_timeframe = InpWorkingTimeframe == PERIOD_CURRENT
                 ? (ENUM_TIMEFRAMES)_Period
                 : InpWorkingTimeframe;
   g_object_prefix = "S019_v04_" + (string)InpMagicNumber + "_";

   if(PeriodSeconds(g_timeframe) <= 0 ||
      InpADRPeriods < 1 ||
      InpLongADRMultiplier <= 0.0 ||
      InpShortADRMultiplier <= 0.0 ||
      InpStopBufferPoints < 0.0 ||
      InpLimitEntryFromStop <= 0.0 ||
      InpLimitEntryFromStop > 1.0 ||
      InpLimitLifetimeBars < 1 ||
      InpTakeProfitRR < 0.0 ||
      InpFastEMAPeriod < 1 ||
      InpSlowEMAPeriod <= InpFastEMAPeriod ||
      InpADXPeriod < 1 ||
      InpATRPeriod < 1 ||
      InpRiskMoney <= 0.0 ||
      InpDailyPositionLimit < 0 ||
      InpDeviationPoints < 0 ||
      InpEntryDelayMinSeconds < 0 ||
      InpEntryDelayMaxSeconds < InpEntryDelayMinSeconds ||
      InpFridayCloseHour < 0 || InpFridayCloseHour > 23 ||
      InpFridayCloseMinute < 0 || InpFridayCloseMinute > 59)
   {
      EaLegacy("operation.error","INVALID_INPUTS",(string)("S-019: invalid input parameters."),"ERROR",__FUNCTION__);
      return INIT_PARAMETERS_INCORRECT;
   }

   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpDeviationPoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   InitEntryDelayRandomizer();
   ResetPendingEntry();
   g_friday_close_logged = false;
   g_daily_limit_logged_day = 0;

   // Live charts keep news TP protection active even during sparse ticks.
   // Strategy Tester stays tick-driven to avoid expensive timer events, unless
   // the entry delay has to fire between ticks.
   if(!MQLInfoInteger(MQL_TESTER) || InpUseEntryDelay)
      g_timer_started = EventSetTimer(1);

   // Slow EMA is a trading filter and must exist in non-visual tests.
   // CreateVisualIndicators() only handles its optional chart display.
   g_slow_ema_handle = iMA(_Symbol, g_timeframe, InpSlowEMAPeriod, 0,
                           MODE_EMA, PRICE_CLOSE);
   if(g_slow_ema_handle == INVALID_HANDLE)
   {
      EaLegacy("data.error","API_OR_BROKER_ERROR",(string)("S-019 v0.4: failed to create Slow EMA trend handle. Error ") + (string)(GetLastError()),"ERROR",__FUNCTION__);
      return INIT_FAILED;
   }

   CreateVisualIndicators();
   if(CalculateLevels())
      DrawLevels();
   else if(true)
      EaLegacy("diagnostic.detail","DETAIL",(string)("S-019: waiting for ") + (string)(InpADRPeriods) + (string)(" completed bars on ") + (string)(EnumToString(g_timeframe)),"INFO",__FUNCTION__);

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_timer_started)
      EventKillTimer();
   ResetPendingEntry();
   DeleteOwnPendingOrders();
   ReleaseVisualIndicators();
   DeleteLevelObjects();
   EaLogDeinit(reason);
}

void OnTimer()
{
   EaLogPulse();
   datetime now = TimeCurrent();
   if(ManageFridayClose(now))
      return;

   ManageNewsProtectionThrottled(now);
   ProcessPendingEntry();
}

void OnTick()
{
   EaLogPulse();
   datetime now = TimeCurrent();
   if(ManageFridayClose(now))
      return;

   ManageNewsProtectionThrottled(now);
   ProcessPendingEntry();

   datetime current_bar = iTime(_Symbol, g_timeframe, 0);
   if(current_bar == 0)
      { EaWait("WORKING_BAR_UNAVAILABLE","","{}","data",""); return; }

   if(!g_levels_ready)
   {
      if(CalculateLevels())
         DrawLevels();
      return;
   }

   // Signals are evaluated exactly once: on the first tick of a new bar.
   if(current_bar != g_reference_bar_time)
   {
      // Remove only orders whose configurable working-TF lifetime elapsed.
      if(!DeleteExpiredPendingOrders())
         return;

      double signal_close = iClose(_Symbol, g_timeframe, 1);
      double signal_long_level = g_long_trigger;
      double signal_short_level = g_short_trigger;
      log_signal_long_level=signal_long_level; log_signal_short_level=signal_short_level;

      // Strict close beyond the level. A wick-only sweep is ignored.
      bool long_signal = signal_close > signal_long_level;
      bool short_signal = signal_close < signal_short_level;

      // A fresh confirmed signal invalidates any older unfilled setup. This
      // prevents simultaneous or opposing pending entries and restarts the
      // lifetime from the newest signal.
      if(long_signal || short_signal)
      {
         CancelPendingEntry("a newer confirmed signal");
         if(!DeleteOwnPendingOrders())
            return;
      }

      double signal_slow_ema = 0.0;
      bool trend_entry_allowed = true;
      if(long_signal || short_signal)
         trend_entry_allowed = SlowEMAAllowsEntry(long_signal,
                                                  short_signal,
                                                  signal_close,
                                                  signal_slow_ema);

      // Recalculate first so a new order uses the latest ADR window,
      // including the signal bar, for its protective stop.
      if(!CalculateLevels())
         { EaWait("ADR_DATA_UNAVAILABLE","","{}","data",""); return; }

      DrawLevels();
      ProcessClosedBarSignal(long_signal, short_signal, signal_close,
                             trend_entry_allowed, signal_slow_ema);
      return;
   }
}

double log_signal_long_level=0.0, log_signal_short_level=0.0;

void OnTradeTransaction(const MqlTradeTransaction &transaction,const MqlTradeRequest &request,const MqlTradeResult &result)
{
   EaLogTransaction(transaction);
}

void OnTrade()
{
   EaRetryDeals();
   EaObserveOrders();
   EaReconcilePositions();
}
