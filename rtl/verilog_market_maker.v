`timescale 1ns/1ps

// Verilog market-maker quote core
module verilog_market_maker #(
    parameter integer BASE_SPREAD_TICKS = 4,
    parameter integer REPLACE_THRESHOLD_TICKS = 2
) (
    input wire clk,
    input wire rst_n,
    input wire calculate_en,
    input wire signed [31:0] mid_ticks,
    input wire signed [31:0] best_bid_ticks,
    input wire signed [31:0] best_ask_ticks,
    input wire signed [31:0] inventory_milli,
    input wire signed [31:0] volatility_bp,
    input wire signed [31:0] bid_qty_milli,
    input wire signed [31:0] ask_qty_milli,
    output reg signed [31:0] quote_bid_ticks,
    output reg signed [31:0] quote_ask_ticks,
    output reg replace_hint,
    output reg cancel_hint,
    output reg done,
    output reg [31:0] latency_cycles
);

    localparam [1:0] IDLE = 2'b00;
    localparam [1:0] COMPUTE = 2'b01;
    localparam [1:0] OUTPUT_STATE = 2'b10;

    reg [1:0] state;
    reg signed [31:0] prev_bid;
    reg signed [31:0] prev_ask;

    reg signed [31:0] spread_ticks;
    reg signed [31:0] half_spread;
    reg signed [31:0] imbalance;
    reg signed [31:0] reservation;
    reg signed [31:0] target_bid;
    reg signed [31:0] target_ask;
    reg signed [31:0] bid_diff;
    reg signed [31:0] ask_diff;

    function signed [31:0] abs32;
        input signed [31:0] value;
        begin
            if (value < 0)
                abs32 = -value;
            else
                abs32 = value;
        end
    endfunction

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            quote_bid_ticks <= 32'sd0;
            quote_ask_ticks <= 32'sd0;
            prev_bid <= 32'sd0;
            prev_ask <= 32'sd0;
            replace_hint <= 1'b0;
            cancel_hint <= 1'b0;
            done <= 1'b0;
            latency_cycles <= 32'd0;
        end else begin
            case (state)
                IDLE: begin
                    done <= 1'b0;
                    replace_hint <= 1'b0;
                    cancel_hint <= 1'b0;
                    latency_cycles <= 32'd0;
                    if (calculate_en) begin
                        state <= COMPUTE;
                        latency_cycles <= latency_cycles + 1;
                    end
                end

                COMPUTE: begin
                    imbalance = ask_qty_milli - bid_qty_milli;
                    spread_ticks = BASE_SPREAD_TICKS + (volatility_bp >>> 5) + (abs32(imbalance) >>> 10);
                    if (spread_ticks < 32'sd1)
                        spread_ticks = 32'sd1;

                    half_spread = spread_ticks >>> 1;
                    reservation = mid_ticks - (inventory_milli >>> 8);

                    target_bid = reservation - half_spread;
                    target_ask = reservation + half_spread;

                    if (target_bid > best_bid_ticks)
                        target_bid = best_bid_ticks;
                    if (target_ask < best_ask_ticks)
                        target_ask = best_ask_ticks;
                    if (target_ask <= target_bid)
                        target_ask = target_bid + 32'sd1;

                    quote_bid_ticks <= target_bid;
                    quote_ask_ticks <= target_ask;
                    latency_cycles <= latency_cycles + 1;
                    state <= OUTPUT_STATE;
                end

                OUTPUT_STATE: begin
                    bid_diff = abs32(quote_bid_ticks - prev_bid);
                    ask_diff = abs32(quote_ask_ticks - prev_ask);

                    if ((bid_diff >= REPLACE_THRESHOLD_TICKS) || (ask_diff >= REPLACE_THRESHOLD_TICKS))
                        replace_hint <= 1'b1;
                    else
                        replace_hint <= 1'b0;

                    if (quote_bid_ticks >= quote_ask_ticks)
                        cancel_hint <= 1'b1;
                    else
                        cancel_hint <= 1'b0;

                    prev_bid <= quote_bid_ticks;
                    prev_ask <= quote_ask_ticks;
                    done <= 1'b1;
                    latency_cycles <= latency_cycles + 1;
                    state <= IDLE;
                end

                default: state <= IDLE;
            endcase
        end
    end

endmodule
