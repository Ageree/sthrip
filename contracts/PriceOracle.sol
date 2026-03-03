// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@chainlink/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title PriceOracle
 * @notice Price feed for ETH/USD and XMR/USD
 */
contract PriceOracle is AccessControl {
    bytes32 public constant ORACLE_ROLE = keccak256("ORACLE_ROLE");
    
    AggregatorV3Interface public immutable ethUsdFeed;
    
    // XMR price (manually updated by oracle network)
    uint256 public xmrPrice;
    uint256 public xmrPriceLastUpdate;
    
    uint256 public constant MAX_PRICE_AGE = 1 hours;
    uint256 public constant MAX_DEVIATION = 500; // 5% in bps
    uint256 public constant PRICE_PRECISION = 1e8;
    
    mapping(address => bool) public authorizedOracles;
    
    event XmrPriceUpdated(uint256 oldPrice, uint256 newPrice, address oracle);
    event OracleAuthorized(address oracle, bool authorized);
    
    constructor(address _ethUsdFeed) {
        require(_ethUsdFeed != address(0), "Invalid feed");
        ethUsdFeed = AggregatorV3Interface(_ethUsdFeed);
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
    }
    
    function authorizeOracle(address oracle, bool authorized) 
        external 
        onlyRole(DEFAULT_ADMIN_ROLE) 
    {
        authorizedOracles[oracle] = authorized;
        emit OracleAuthorized(oracle, authorized);
    }
    
    function updateXmrPrice(uint256 newPrice) external {
        require(authorizedOracles[msg.sender], "Not authorized");
        require(newPrice > 0, "Invalid price");
        
        // Check deviation
        if (xmrPrice > 0) {
            uint256 deviation = _calculateDeviation(xmrPrice, newPrice);
            require(deviation <= MAX_DEVIATION, "Price deviation too high");
        }
        
        uint256 oldPrice = xmrPrice;
        xmrPrice = newPrice;
        xmrPriceLastUpdate = block.timestamp;
        
        emit XmrPriceUpdated(oldPrice, newPrice, msg.sender);
    }
    
    function getEthPrice() public view returns (uint256) {
        (, int256 price,,,) = ethUsdFeed.latestRoundData();
        require(price > 0, "Invalid ETH price");
        return uint256(price);
    }
    
    function getXmrPrice() public view returns (uint256) {
        require(block.timestamp - xmrPriceLastUpdate <= MAX_PRICE_AGE, "XMR price stale");
        return xmrPrice;
    }
    
    /**
     * @notice Get XMR/ETH exchange rate
     * @return rate XMR price in ETH (18 decimals)
     */
    function getXmrToEthRate() external view returns (uint256) {
        uint256 ethUsd = getEthPrice();
        uint256 xmrUsd = getXmrPrice();
        
        // rate = (XMR/USD) / (ETH/USD) = XMR/ETH
        // Both prices have 8 decimals, result needs 18 decimals
        return (xmrUsd * 1e18) / ethUsd;
    }
    
    /**
     * @notice Calculate ETH amount for given XMR amount
     */
    function quoteXmrToEth(uint256 xmrAmount) external view returns (uint256) {
        uint256 rate = this.getXmrToEthRate();
        return (xmrAmount * rate) / 1e12; // XMR has 12 decimals
    }
    
    function _calculateDeviation(uint256 old, uint256 new_) 
        internal 
        pure 
        returns (uint256) 
    {
        if (old > new_) {
            return ((old - new_) * 10000) / old;
        } else {
            return ((new_ - old) * 10000) / old;
        }
    }
}
